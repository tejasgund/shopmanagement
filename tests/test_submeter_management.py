"""
Tests for the Submeters module: registering a meter without a shop, assigning
and unassigning it, the history view, and the guards that stop a meter being
moved once its readings have been billed.
"""

from decimal import Decimal

import pytest

from conftest import make_jpeg
from create_tables import Meter, MeterReading


def _submit(client, auth, meter_id, reading):
    return client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter_id), "customer_reading": str(reading)},
        files={"photo": ("m.jpg", make_jpeg(), "image/jpeg")},
        headers=auth,
    )


# ══════════════════════════════════════════════════════════════════════════════
# UNASSIGNED METERS
# ══════════════════════════════════════════════════════════════════════════════

def test_meter_can_be_created_without_a_shop(client, admin_auth):
    resp = client.post("/api/meters", headers=admin_auth,
                       json={"meter_number": "SPARE-01", "initial_reading": 0})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_assigned"] is False
    assert body["shop_id"] is None
    assert body["shop_number"] is None


def test_duplicate_unassigned_meter_number_is_refused(client, admin_auth):
    client.post("/api/meters", headers=admin_auth, json={"meter_number": "SPARE-01"})
    resp = client.post("/api/meters", headers=admin_auth, json={"meter_number": "SPARE-01"})
    assert resp.status_code == 409


def test_meters_can_be_filtered_by_assignment(client, admin_auth, meter):
    client.post("/api/meters", headers=admin_auth, json={"meter_number": "SPARE-01"})

    assigned = client.get("/api/meters?assigned=true", headers=admin_auth).json()
    unassigned = client.get("/api/meters?assigned=false", headers=admin_auth).json()

    assert [m["meter_number"] for m in assigned] == ["MTR-001"]
    assert [m["meter_number"] for m in unassigned] == ["SPARE-01"]


def test_tenant_never_sees_an_unassigned_meter(client, admin_auth, tenant_auth):
    client.post("/api/meters", headers=admin_auth, json={"meter_number": "SPARE-01"})
    assert client.get("/api/tenant/meters", headers=tenant_auth).json() == []


def test_tenant_cannot_submit_against_an_unassigned_meter(
    client, admin_auth, tenant_auth, photo_dir
):
    m = client.post("/api/meters", headers=admin_auth,
                    json={"meter_number": "SPARE-01"}).json()
    resp = _submit(client, tenant_auth, m["id"], 100)
    assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# ASSIGN / UNASSIGN
# ══════════════════════════════════════════════════════════════════════════════

def test_assigning_a_meter_makes_it_visible_to_the_tenant(
    client, admin_auth, tenant_auth, shop
):
    m = client.post("/api/meters", headers=admin_auth,
                    json={"meter_number": "SPARE-01", "initial_reading": 500}).json()
    assert client.get("/api/tenant/meters", headers=tenant_auth).json() == []

    resp = client.post(f"/api/meters/{m['id']}/assign-shop", headers=admin_auth,
                       json={"shop_id": shop.id})
    assert resp.status_code == 200
    assert resp.json()["meter"]["is_assigned"] is True

    tenant_view = client.get("/api/tenant/meters", headers=tenant_auth).json()
    assert len(tenant_view) == 1
    assert tenant_view[0]["meter_number"] == "SPARE-01"
    assert tenant_view[0]["previous_reading"] == 500.0


def test_assigning_to_a_shop_that_already_has_that_number_is_refused(
    client, admin_auth, shop, meter
):
    m = client.post("/api/meters", headers=admin_auth,
                    json={"meter_number": meter.meter_number}).json()
    resp = client.post(f"/api/meters/{m['id']}/assign-shop", headers=admin_auth,
                       json={"shop_id": shop.id})
    assert resp.status_code == 409


def test_assigning_to_a_missing_shop_is_refused(client, admin_auth):
    m = client.post("/api/meters", headers=admin_auth, json={"meter_number": "SPARE-01"}).json()
    resp = client.post(f"/api/meters/{m['id']}/assign-shop", headers=admin_auth,
                       json={"shop_id": 9999})
    assert resp.status_code == 404


def test_unassign_returns_a_meter_to_the_pool(client, admin_auth, tenant_auth, meter):
    resp = client.post(f"/api/meters/{meter.id}/unassign", headers=admin_auth)
    assert resp.status_code == 200
    assert resp.json()["meter"]["is_assigned"] is False
    assert client.get("/api/tenant/meters", headers=tenant_auth).json() == []


def test_unassign_is_blocked_while_a_reading_is_pending(
    client, admin_auth, tenant_auth, meter, photo_dir
):
    _submit(client, tenant_auth, meter.id, 12732)
    resp = client.post(f"/api/meters/{meter.id}/unassign", headers=admin_auth)
    assert resp.status_code == 400
    assert "waiting for review" in resp.json()["detail"]


def test_unassign_keeps_past_readings_and_bills(
    client, admin_auth, tenant_auth, meter, photo_dir, tariff, db
):
    rid = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{rid}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    client.post(f"/api/meters/{meter.id}/unassign", headers=admin_auth)

    db.expire_all()
    reading = db.get(MeterReading, rid)
    assert reading.status == "approved"
    assert reading.shop_id is not None       # history still records the shop
    assert reading.bill_id is not None


def test_a_meter_with_billed_history_cannot_be_moved(
    client, admin_auth, tenant_auth, meter, photo_dir, tariff, other_shop
):
    rid = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{rid}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    resp = client.post(f"/api/meters/{meter.id}/assign-shop", headers=admin_auth,
                       json={"shop_id": other_shop.id})
    assert resp.status_code == 400
    assert "can't be moved" in resp.json()["detail"]


def test_reassigning_to_the_same_shop_is_refused(client, admin_auth, meter, shop):
    resp = client.post(f"/api/meters/{meter.id}/assign-shop", headers=admin_auth,
                       json={"shop_id": shop.id})
    assert resp.status_code == 400
    assert "already on shop" in resp.json()["detail"]


def test_tenant_cannot_assign_or_unassign(client, tenant_auth, meter, shop):
    assert client.post(f"/api/meters/{meter.id}/assign-shop",
                       json={"shop_id": shop.id}, headers=tenant_auth).status_code == 403
    assert client.post(f"/api/meters/{meter.id}/unassign",
                       headers=tenant_auth).status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# CURRENT READING / LAST UPDATED
# ══════════════════════════════════════════════════════════════════════════════

def test_current_reading_starts_at_the_installed_value(client, admin_auth, meter):
    body = client.get(f"/api/meters/{meter.id}", headers=admin_auth).json()
    assert body["current_reading"] == 12450.0
    assert body["last_updated"] is None
    assert body["reading_count"] == 0


def test_current_reading_and_last_updated_follow_approval(
    client, admin_auth, tenant_auth, meter, photo_dir, tariff
):
    rid = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]

    mid_flight = client.get(f"/api/meters/{meter.id}", headers=admin_auth).json()
    assert mid_flight["has_pending_reading"] is True
    assert mid_flight["current_reading"] == 12450.0        # unchanged until approved

    client.post(f"/api/meter-readings/{rid}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    after = client.get(f"/api/meters/{meter.id}", headers=admin_auth).json()
    assert after["current_reading"] == 12732.0
    assert after["last_updated"] is not None
    assert after["reading_count"] == 1
    assert after["has_pending_reading"] is False


# ══════════════════════════════════════════════════════════════════════════════
# HISTORY VIEW
# ══════════════════════════════════════════════════════════════════════════════

def test_history_is_empty_for_a_new_meter(client, admin_auth, meter):
    body = client.get(f"/api/meters/{meter.id}/history", headers=admin_auth).json()
    assert body["readings"] == []
    assert body["summary"]["total_readings"] == 0
    assert body["summary"]["total_units"] == 0
    assert body["meter"]["meter_number"] == "MTR-001"


def test_history_summarises_every_reading(
    client, admin_auth, tenant_auth, meter, photo_dir, tariff
):
    # 1) approved
    r1 = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{r1}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)
    # 2) rejected
    r2 = _submit(client, tenant_auth, meter.id, 12800).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{r2}/reject",
                json={"reason": "Blurry"}, headers=admin_auth)
    # 3) still pending
    _submit(client, tenant_auth, meter.id, 12900)

    body = client.get(f"/api/meters/{meter.id}/history", headers=admin_auth).json()
    s = body["summary"]

    assert s["total_readings"] == 3
    assert s["approved_count"] == 1
    assert s["rejected_count"] == 1
    assert s["pending_count"] == 1
    assert s["total_units"] == 282.0
    assert s["total_billed"] == 2679.0
    assert s["average_units_per_reading"] == 282.0

    newest = body["readings"][0]
    assert newest["status"] == "pending"           # newest first
    assert newest["has_photo"] is True


def test_history_shows_units_and_amount_per_reading(
    client, admin_auth, tenant_auth, meter, photo_dir, tariff
):
    rid = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{rid}/approve",
                json={"admin_verified_reading": 12730,
                      "override_reason": "Photo shows 12730"},
                headers=admin_auth)

    entry = client.get(f"/api/meters/{meter.id}/history", headers=admin_auth).json()["readings"][0]
    assert entry["units"] == 280.0
    assert entry["amount"] == 2660.0
    assert entry["previous_reading"] == 12450.0
    assert entry["customer_reading"] == 12732.0          # what the tenant sent
    assert entry["admin_verified_reading"] == 12730.0    # what you corrected it to
    assert entry["override_reason"] == "Photo shows 12730"


def test_tenant_cannot_read_meter_history(client, tenant_auth, meter):
    assert client.get(f"/api/meters/{meter.id}/history",
                      headers=tenant_auth).status_code == 403


def test_history_of_a_missing_meter_is_404(client, admin_auth):
    assert client.get("/api/meters/9999/history", headers=admin_auth).status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# SHOP-CREATE FLOW (the frontend posts the shop, then the meter)
# ══════════════════════════════════════════════════════════════════════════════

def test_creating_a_shop_then_its_meter(client, admin_auth):
    shop = client.post("/api/shop", headers=admin_auth, json={
        "shop_number": "D-404", "area_sqft": 300, "shop_rent": 9000, "shop_deposit": 45000,
    }).json()

    resp = client.post("/api/meters", headers=admin_auth, json={
        "shop_id": shop["id"], "meter_number": "MTR-404",
        "meter_type": "electricity", "initial_reading": 100,
    })
    assert resp.status_code == 201
    assert resp.json()["shop_number"] == "D-404"
    assert resp.json()["current_reading"] == 100.0


def test_a_shop_can_have_several_meters(client, admin_auth, shop, meter):
    second = client.post("/api/meters", headers=admin_auth, json={
        "shop_id": shop.id, "meter_number": "MTR-002", "initial_reading": 0,
    })
    assert second.status_code == 201
    on_shop = client.get(f"/api/meters?shop_id={shop.id}", headers=admin_auth).json()
    assert {m["meter_number"] for m in on_shop} == {"MTR-001", "MTR-002"}


def test_tenant_sees_all_meters_on_their_shop(client, admin_auth, tenant_auth, shop, meter):
    client.post("/api/meters", headers=admin_auth, json={
        "shop_id": shop.id, "meter_number": "MTR-002", "initial_reading": 0,
    })
    assert len(client.get("/api/tenant/meters", headers=tenant_auth).json()) == 2
