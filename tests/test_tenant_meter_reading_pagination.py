"""
Tests for GET /api/tenant/meters/{meter_id}/readings - the paginated,
single-meter reading history endpoint added alongside (not instead of)
GET /api/tenant/meter-readings and the /api/tenant/home bundle.

The rules these lock down, in priority order:
  1. A tenant can only ever page through readings on THEIR OWN meter - a
     meter on someone else's shop, or an unassigned meter, must 404 (never
     leak existence via a 403).
  2. Pagination math (page/limit/total, offset) must be correct and stable,
     matching the same convention already used by GET /api/bills.
  3. Ordering is newest-first and stable, so "page 2" never repeats or skips
     a row that was already on page 1.
  4. Readings from a DIFFERENT meter (even one on the same tenant's own shop)
     must never leak into this meter's page - this is the whole point of the
     endpoint (the /api/tenant/home bundle's capped, cross-meter list can
     let one meter's history crowd out another's; this endpoint can't).
  5. limit is bounded (1-100) - an out-of-range value is rejected, not
     silently clamped or allowed to blow up the query.
"""
from datetime import datetime, timedelta

import pytest

from models.schema import Meter, MeterReading


def _add_reading(db, meter, tenant, *, day, status="approved", customer_reading=100):
    r = MeterReading(
        meter_id=meter.id, shop_id=meter.shop_id, user_id=tenant.id,
        previous_reading=0, customer_reading=customer_reading,
        approved_reading=customer_reading if status == "approved" else None,
        status=status,
        reading_date=datetime(2026, 1, 1) + timedelta(days=day),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ══════════════════════════════════════════════════════════════════════════════
# OWNERSHIP / IDOR
# ══════════════════════════════════════════════════════════════════════════════

def test_other_tenants_meter_is_404_not_403(client, other_tenant_auth, meter, db, tenant):
    """meter belongs to `shop`, which belongs to `tenant` - other_tenant must
    not be able to see it, and the failure must not distinguish "exists but
    not yours" from "doesn't exist"."""
    _add_reading(db, meter, tenant, day=0)
    res = client.get(f"/api/tenant/meters/{meter.id}/readings", headers=other_tenant_auth)
    assert res.status_code == 404


def test_unassigned_meter_is_404(client, tenant_auth, db):
    """A meter with no shop belongs to nobody - not even the tenant who
    happens to know its ID."""
    m = Meter(shop_id=None, meter_number="SPARE-1", meter_type="electricity",
              initial_reading=0, is_active=True)
    db.add(m); db.commit(); db.refresh(m)
    res = client.get(f"/api/tenant/meters/{m.id}/readings", headers=tenant_auth)
    assert res.status_code == 404


def test_nonexistent_meter_is_404(client, tenant_auth):
    res = client.get("/api/tenant/meters/999999/readings", headers=tenant_auth)
    assert res.status_code == 404


def test_owner_can_see_their_own_meter_readings(client, tenant_auth, meter, db, tenant):
    _add_reading(db, meter, tenant, day=0)
    res = client.get(f"/api/tenant/meters/{meter.id}/readings", headers=tenant_auth)
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# PAGINATION CORRECTNESS
# ══════════════════════════════════════════════════════════════════════════════

def test_pagination_math_and_ordering(client, tenant_auth, meter, db, tenant):
    """25 readings, newest first, page size 10 -> 3 pages (10, 10, 5), no
    row repeated or skipped across pages."""
    created = [_add_reading(db, meter, tenant, day=i, customer_reading=100 + i) for i in range(25)]
    expected_order = [r.id for r in sorted(created, key=lambda r: r.reading_date, reverse=True)]

    seen_ids = []
    for page in (1, 2, 3):
        res = client.get(f"/api/tenant/meters/{meter.id}/readings",
                          params={"page": page, "limit": 10}, headers=tenant_auth)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["page"] == page
        assert body["limit"] == 10
        assert body["total"] == 25
        seen_ids.extend(row["id"] for row in body["data"])

    assert len(body["data"]) == 5          # last page: 25 - 10 - 10
    assert seen_ids == expected_order      # exact order, no dupes, no gaps
    assert len(set(seen_ids)) == 25


def test_default_page_and_limit(client, tenant_auth, meter, db, tenant):
    _add_reading(db, meter, tenant, day=0)
    res = client.get(f"/api/tenant/meters/{meter.id}/readings", headers=tenant_auth)
    body = res.json()
    assert body["page"] == 1
    assert body["limit"] == 20


def test_empty_meter_returns_empty_page_not_error(client, tenant_auth, meter):
    """A brand-new meter with zero readings must be a normal empty page, not a 404/500."""
    res = client.get(f"/api/tenant/meters/{meter.id}/readings", headers=tenant_auth)
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 0
    assert body["data"] == []
    assert body["meter"]["id"] == meter.id


def test_limit_out_of_range_rejected(client, tenant_auth, meter):
    res = client.get(f"/api/tenant/meters/{meter.id}/readings",
                      params={"limit": 500}, headers=tenant_auth)
    assert res.status_code == 422
    res2 = client.get(f"/api/tenant/meters/{meter.id}/readings",
                       params={"limit": 0}, headers=tenant_auth)
    assert res2.status_code == 422


def test_status_filter(client, tenant_auth, meter, db, tenant):
    _add_reading(db, meter, tenant, day=0, status="approved")
    _add_reading(db, meter, tenant, day=1, status="pending")
    res = client.get(f"/api/tenant/meters/{meter.id}/readings",
                      params={"status": "pending"}, headers=tenant_auth)
    body = res.json()
    assert body["total"] == 1
    assert body["data"][0]["status"] == "pending"


# ══════════════════════════════════════════════════════════════════════════════
# NO CROSS-METER LEAKAGE (the reason this endpoint exists)
# ══════════════════════════════════════════════════════════════════════════════

def test_readings_from_a_different_meter_never_appear(client, tenant_auth, meter, shop, db, tenant):
    """A second meter on the SAME shop must not leak its readings into the
    first meter's page - this is the exact crowding bug the bundle's shared,
    cross-meter capped list has, and the reason for a per-meter endpoint."""
    other_meter = Meter(shop_id=shop.id, meter_number="MTR-002", meter_type="electricity",
                        initial_reading=0, is_active=True)
    db.add(other_meter); db.commit(); db.refresh(other_meter)

    _add_reading(db, meter, tenant, day=0, customer_reading=111)
    for i in range(5):
        _add_reading(db, other_meter, tenant, day=i, customer_reading=200 + i)

    res = client.get(f"/api/tenant/meters/{meter.id}/readings", headers=tenant_auth)
    body = res.json()
    assert body["total"] == 1
    assert body["data"][0]["customer_reading"] == 111.0


def test_response_includes_meter_and_bill_shape_matching_existing_reading_dict(
    client, tenant_auth, meter, db, tenant,
):
    """The row shape must match _reading_to_dict exactly (same fields other
    tenant screens already rely on: photo_url, bill, has_photo, etc.) - this
    is a new endpoint, not a new response format."""
    _add_reading(db, meter, tenant, day=0)
    res = client.get(f"/api/tenant/meters/{meter.id}/readings", headers=tenant_auth)
    row = res.json()["data"][0]
    for key in ("id", "meter_id", "meter_number", "shop_id", "shop_number",
                "customer_reading", "reading_date", "status", "has_photo",
                "photo_url", "bill_id"):
        assert key in row
    # Admin-only fields must never leak to a tenant through this endpoint either.
    assert "admin_verified_reading" not in row
    assert "admin_note" not in row
