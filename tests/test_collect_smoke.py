"""
Tests for POST /api/meter-readings/collect - an admin submitting a meter
reading on a tenant's behalf (e.g. the tenant can't use the app). Mirrors
the tenant-submission rules in test_meter_readings.py: same validation,
same photo handling, same review queue - just a different submitter.
"""
from conftest import make_jpeg
from create_tables import MeterReading


def test_admin_can_collect_reading_for_tenant(client, db, admin_auth, tenant, shop, meter):
    res = client.post(
        "/api/meter-readings/collect",
        data={"meter_id": meter.id, "customer_reading": "12732", "customer_note": "phone call"},
        files={"photo": ("meter.jpg", make_jpeg(), "image/jpeg")},
        headers=admin_auth,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["success"] is True
    reading = body["reading"]
    assert reading["status"] == "pending"
    assert reading["user_name"] == tenant.name          # belongs to the TENANT, not the admin
    assert reading["has_photo"] is True

    row = db.query(MeterReading).filter(MeterReading.id == reading["id"]).first()
    assert row.user_id == tenant.id
    assert row.collected_by is not None                  # recorded who submitted it


def test_collect_without_tenant_assigned_is_rejected(client, admin_auth, db):
    from create_tables import Shop, Meter
    lonely_shop = Shop(shop_number="Z-999", status="available", shop_rent=1000, shop_deposit=1000)
    db.add(lonely_shop)
    db.commit()
    db.refresh(lonely_shop)
    lonely_meter = Meter(shop_id=lonely_shop.id, meter_number="MTR-999",
                          meter_type="electricity", initial_reading=0, is_active=True)
    db.add(lonely_meter)
    db.commit()
    db.refresh(lonely_meter)

    res = client.post(
        "/api/meter-readings/collect",
        data={"meter_id": lonely_meter.id, "customer_reading": "100"},
        headers=admin_auth,
    )
    assert res.status_code == 400
    assert "tenant" in res.json()["detail"].lower()


def test_collect_forbidden_for_tenants(client, tenant_auth, meter):
    res = client.post(
        "/api/meter-readings/collect",
        data={"meter_id": meter.id, "customer_reading": "12732"},
        headers=tenant_auth,
    )
    assert res.status_code == 403


def test_collect_duplicate_pending_is_blocked(client, admin_auth, meter):
    first = client.post(
        "/api/meter-readings/collect",
        data={"meter_id": meter.id, "customer_reading": "12700"},
        files={"photo": ("meter.jpg", make_jpeg(), "image/jpeg")},
        headers=admin_auth,
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/meter-readings/collect",
        data={"meter_id": meter.id, "customer_reading": "12750"},
        files={"photo": ("meter.jpg", make_jpeg(), "image/jpeg")},
        headers=admin_auth,
    )
    assert second.status_code == 409


def test_collect_route_no_longer_shadows_get_by_id(client, admin_auth, meter, tenant, db):
    """Regression check: the new literal '/collect' route must not break
    GET /api/meter-readings/{id} for numeric ids."""
    from datetime import datetime
    r = MeterReading(meter_id=meter.id, shop_id=meter.shop_id, user_id=tenant.id,
                      previous_reading=12450, customer_reading=12700, status="pending",
                      reading_date=datetime(2026, 5, 1))
    db.add(r)
    db.commit()
    db.refresh(r)

    res = client.get(f"/api/meter-readings/{r.id}", headers=admin_auth)
    assert res.status_code == 200
