"""
Tests for the submeter reading workflow.

The rules these lock down, in priority order:
  1. A bill is only ever calculated from the ADMIN's verified reading.
  2. One approved reading produces at most one bill, even on double-click.
  3. A reading below the previous approved one never becomes a bill.
  4. Bills keep the tariff that was live when they were raised.
  5. A tenant can only ever see their own readings, photos and bills.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from conftest import make_jpeg, make_png
from create_tables import Bill, Meter, MeterReading, MeterTariff


# ══════════════════════════════════════════════════════════════════════════════
# CONSUMPTION MATHS
# ══════════════════════════════════════════════════════════════════════════════

def test_units_are_current_minus_previous():
    from meter_service import calculate_units
    assert calculate_units(Decimal("12450"), Decimal("12732")) == Decimal("282.00")


def test_reading_below_previous_is_refused():
    """The headline validation rule - a negative bill must never be possible."""
    from meter_service import MeterError, calculate_units
    with pytest.raises(MeterError) as exc:
        calculate_units(Decimal("12450"), Decimal("12300"))
    assert "lower than" in str(exc.value)


def test_equal_readings_give_zero_units():
    from meter_service import calculate_units
    assert calculate_units(Decimal("12450"), Decimal("12450")) == Decimal("0.00")


# ══════════════════════════════════════════════════════════════════════════════
# PREVIOUS READING LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def test_first_reading_uses_meter_initial_reading(db, meter):
    """With no history, the first bill counts up from installation, not from 0."""
    from meter_service import previous_reading_value
    assert previous_reading_value(db, meter) == Decimal("12450.00")


def test_pending_and_rejected_readings_are_not_used_as_previous(db, meter, tenant):
    """Only APPROVED readings may act as the basis for the next bill."""
    from meter_service import previous_reading_value

    db.add(MeterReading(
        meter_id=meter.id, shop_id=meter.shop_id, user_id=tenant.id,
        previous_reading=12450, customer_reading=13000, status="pending",
        reading_date=datetime(2026, 5, 1),
    ))
    db.add(MeterReading(
        meter_id=meter.id, shop_id=meter.shop_id, user_id=tenant.id,
        previous_reading=12450, customer_reading=14000, status="rejected",
        reading_date=datetime(2026, 5, 2),
    ))
    db.commit()

    assert previous_reading_value(db, meter) == Decimal("12450.00")


def test_approved_reading_becomes_the_next_previous(db, meter, tenant):
    from meter_service import previous_reading_value

    db.add(MeterReading(
        meter_id=meter.id, shop_id=meter.shop_id, user_id=tenant.id,
        previous_reading=12450, customer_reading=12732,
        approved_reading=12732, status="approved",
        reading_date=datetime(2026, 5, 1),
    ))
    db.commit()

    assert previous_reading_value(db, meter) == Decimal("12732.00")


# ══════════════════════════════════════════════════════════════════════════════
# TARIFF - HISTORICAL PRICING
# ══════════════════════════════════════════════════════════════════════════════

def test_tariff_lookup_picks_the_rate_live_on_that_date(db):
    from meter_service import applicable_tariff

    for month, price in ((4, "8.00"), (5, "8.50"), (6, "9.00"), (7, "9.50")):
        db.add(MeterTariff(meter_type="electricity", unit_price=Decimal(price),
                           effective_from=datetime(2026, month, 1)))
    db.commit()

    assert applicable_tariff(db, "electricity", datetime(2026, 6, 15)).unit_price == Decimal("9.0000")
    assert applicable_tariff(db, "electricity", datetime(2026, 4, 30)).unit_price == Decimal("8.0000")
    # A date after the newest rate keeps using that newest rate.
    assert applicable_tariff(db, "electricity", datetime(2026, 12, 1)).unit_price == Decimal("9.5000")


def test_no_tariff_before_first_effective_date(db):
    from meter_service import applicable_tariff
    db.add(MeterTariff(meter_type="electricity", unit_price=Decimal("9.50"),
                       effective_from=datetime(2026, 7, 1)))
    db.commit()
    assert applicable_tariff(db, "electricity", datetime(2026, 6, 1)) is None


def test_estimate_applies_fixed_charge_and_tax(db, meter):
    from meter_service import estimate_bill
    db.add(MeterTariff(meter_type="electricity", unit_price=Decimal("10.00"),
                       fixed_charge=Decimal("100"), tax_percent=Decimal("10"),
                       effective_from=datetime(2000, 1, 1)))
    db.commit()

    result = estimate_bill(db, meter, Decimal("100"), datetime(2026, 6, 1))
    assert result["energy_charge"] == 1000.0          # 100 units x 10.00
    assert result["subtotal"] == 1100.0               # + 100 fixed
    assert result["tax_amount"] == 110.0              # 10% of 1100
    assert result["total"] == 1210.0


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER SUBMISSION
# ══════════════════════════════════════════════════════════════════════════════

def _submit(client, auth, meter_id, reading, photo=True, note=None):
    files = {"photo": ("meter.jpg", make_jpeg(), "image/jpeg")} if photo else None
    data = {"meter_id": str(meter_id), "customer_reading": str(reading)}
    if note:
        data["customer_note"] = note
    return client.post("/api/tenant/meter-readings", data=data, files=files, headers=auth)


def test_tenant_can_submit_reading_with_photo(client, tenant_auth, meter, photo_dir):
    resp = _submit(client, tenant_auth, meter.id, 12732)
    assert resp.status_code == 201, resp.text

    body = resp.json()["reading"]
    assert body["status"] == "pending"
    assert body["customer_reading"] == 12732.0
    assert body["previous_reading"] == 12450.0
    assert body["has_photo"] is True
    # No bill is created at submission time.
    assert body["bill_id"] is None


def test_photo_is_stored_on_disk(client, tenant_auth, meter, photo_dir, db):
    _submit(client, tenant_auth, meter.id, 12732)
    reading = db.query(MeterReading).first()
    assert reading.photo_path
    assert (photo_dir / reading.photo_path).is_file()


def test_submission_rejected_below_previous_reading(client, tenant_auth, meter, photo_dir):
    resp = _submit(client, tenant_auth, meter.id, 12000)
    assert resp.status_code == 400
    assert "lower than" in resp.json()["detail"]


def test_non_image_upload_is_refused(client, tenant_auth, meter, photo_dir):
    """Renaming a non-image to .jpg must not get past the magic-byte check."""
    fake = b"MZ\x90\x00" + b"\x41" * 4096          # PE header, .jpg extension
    resp = client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter.id), "customer_reading": "12732"},
        files={"photo": ("evil.jpg", fake, "image/jpeg")},
        headers=tenant_auth,
    )
    assert resp.status_code == 400
    assert "does not look like a photo" in resp.json()["detail"]


def test_truncated_photo_is_refused(client, tenant_auth, meter, photo_dir):
    resp = client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter.id), "customer_reading": "12732"},
        files={"photo": ("tiny.jpg", b"\xff\xd8\xff", "image/jpeg")},
        headers=tenant_auth,
    )
    assert resp.status_code == 400
    assert "corrupted or incomplete" in resp.json()["detail"]


def test_png_photos_are_accepted(client, tenant_auth, meter, photo_dir):
    resp = client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter.id), "customer_reading": "12732"},
        files={"photo": ("meter.png", make_png(), "image/png")},
        headers=tenant_auth,
    )
    assert resp.status_code == 201


def test_oversized_photo_is_refused(client, tenant_auth, meter, photo_dir, db):
    import settings_service
    settings_service.set_many(db, {"meter.photo_max_mb": 1})
    db.commit()
    settings_service.invalidate_cache()

    big = make_jpeg(2 * 1024 * 1024)   # 2 MB against a 1 MB limit
    resp = client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter.id), "customer_reading": "12732"},
        files={"photo": ("big.jpg", big, "image/jpeg")},
        headers=tenant_auth,
    )
    assert resp.status_code == 400
    assert "over the 1 MB limit" in resp.json()["detail"]


def test_photo_required_by_default(client, tenant_auth, meter, photo_dir):
    resp = _submit(client, tenant_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 400
    assert "photo" in resp.json()["detail"].lower()


def test_second_pending_submission_is_blocked(client, tenant_auth, meter, photo_dir):
    assert _submit(client, tenant_auth, meter.id, 12732).status_code == 201
    resp = _submit(client, tenant_auth, meter.id, 12800)
    assert resp.status_code == 409
    assert "already waiting" in resp.json()["detail"]


def test_tenant_cannot_submit_against_another_tenants_meter(
    client, other_tenant_auth, meter, photo_dir
):
    resp = _submit(client, other_tenant_auth, meter.id, 12732)
    assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# ACCESS CONTROL / IDOR
# ══════════════════════════════════════════════════════════════════════════════

def test_tenant_cannot_read_another_tenants_reading(
    client, tenant_auth, other_tenant_auth, meter, photo_dir
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]

    # Owner can see it.
    assert client.get(f"/api/tenant/meter-readings/{reading_id}",
                      headers=tenant_auth).status_code == 200
    # Someone else gets a 404, not a 403 - the response must not confirm the row exists.
    assert client.get(f"/api/tenant/meter-readings/{reading_id}",
                      headers=other_tenant_auth).status_code == 404


def test_tenant_cannot_view_another_tenants_photo(
    client, tenant_auth, other_tenant_auth, meter, photo_dir
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    assert client.get(f"/api/meter-readings/{reading_id}/photo",
                      headers=tenant_auth).status_code == 200
    assert client.get(f"/api/meter-readings/{reading_id}/photo",
                      headers=other_tenant_auth).status_code == 404


def test_admin_can_view_any_photo(client, tenant_auth, admin_auth, meter, photo_dir):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.get(f"/api/meter-readings/{reading_id}/photo", headers=admin_auth)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_tenant_cannot_approve(client, tenant_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(f"/api/meter-readings/{reading_id}/approve",
                       json={"admin_verified_reading": 12732}, headers=tenant_auth)
    assert resp.status_code == 403


def test_tenant_cannot_list_the_admin_review_queue(client, tenant_auth):
    assert client.get("/api/meter-readings", headers=tenant_auth).status_code == 403


def test_tenant_cannot_manage_meters_or_tariffs(client, tenant_auth, shop):
    assert client.get("/api/meters", headers=tenant_auth).status_code == 403
    assert client.post("/api/meters", json={"shop_id": shop.id, "meter_number": "X"},
                       headers=tenant_auth).status_code == 403
    assert client.get("/api/meter-tariffs", headers=tenant_auth).status_code == 403


def test_unauthenticated_requests_are_refused(client, meter):
    assert client.get("/api/meter-readings").status_code in (401, 403)
    assert client.get("/api/tenant/meters").status_code in (401, 403)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN REVIEW
# ══════════════════════════════════════════════════════════════════════════════

def test_admin_review_screen_has_photo_previous_and_customer_reading(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    body = client.get(f"/api/meter-readings/{reading_id}", headers=admin_auth).json()

    assert body["photo_url"] == f"/api/meter-readings/{reading_id}/photo"
    assert body["customer_reading"] == 12732.0
    assert body["review"]["previous_reading"] == 12450.0
    assert body["review"]["provisional_units_from_customer"] == 282.0
    # 282 units x 9.50
    assert body["review"]["provisional_estimate_from_customer"]["total"] == 2679.0


def test_preview_does_not_change_anything(client, tenant_auth, admin_auth, meter, photo_dir, tariff, db):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(f"/api/meter-readings/{reading_id}/preview",
                       json={"admin_verified_reading": 12730}, headers=admin_auth)
    body = resp.json()

    assert body["valid"] is True
    assert body["units"] == 280.0
    assert body["comparison"]["matches"] is False
    assert body["comparison"]["difference"] == -2.0

    db.expire_all()
    assert db.get(MeterReading, reading_id).status == "pending"


def test_preview_flags_a_reading_below_previous(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    body = client.post(f"/api/meter-readings/{reading_id}/preview",
                       json={"admin_verified_reading": 12000}, headers=admin_auth).json()
    assert body["valid"] is False
    assert "lower than" in body["error"]


def test_comparison_reports_a_match(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    body = client.post(f"/api/meter-readings/{reading_id}/preview",
                       json={"admin_verified_reading": 12732}, headers=admin_auth).json()
    assert body["comparison"]["matches"] is True


def test_zero_consumption_raises_a_warning(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 12450).json()["reading"]["id"]
    body = client.post(f"/api/meter-readings/{reading_id}/preview",
                       json={"admin_verified_reading": 12450}, headers=admin_auth).json()
    assert "zero_consumption" in [a["code"] for a in body["anomalies"]]


def test_high_consumption_raises_a_warning(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 19450).json()["reading"]["id"]
    body = client.post(f"/api/meter-readings/{reading_id}/preview",
                       json={"admin_verified_reading": 19450}, headers=admin_auth).json()
    assert "high_consumption" in [a["code"] for a in body["anomalies"]]
    # A warning must not block approval.
    assert body["valid"] is True


def test_verify_saves_admin_reading_without_approving(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.patch(f"/api/meter-readings/{reading_id}/verify",
                        json={"admin_verified_reading": 12730}, headers=admin_auth)
    assert resp.status_code == 200

    db.expire_all()
    reading = db.get(MeterReading, reading_id)
    assert reading.admin_verified_reading == Decimal("12730.00")
    assert reading.status == "pending"
    assert reading.bill_id is None


# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL -> BILL
# ══════════════════════════════════════════════════════════════════════════════

def test_approval_creates_one_bill_from_the_admin_reading(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db, tenant
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]

    resp = client.post(f"/api/meter-readings/{reading_id}/approve",
                       json={"admin_verified_reading": 12732}, headers=admin_auth)
    assert resp.status_code == 200, resp.text

    result = resp.json()["result"]
    assert result["units"] == 282.0
    assert result["bill_created"] is True

    db.expire_all()
    reading = db.get(MeterReading, reading_id)
    assert reading.status == "approved"
    assert reading.approved_reading == Decimal("12732.00")

    bills = db.query(Bill).all()
    assert len(bills) == 1
    assert bills[0].amount == Decimal("2679.00")     # 282 x 9.50
    assert bills[0].user_id == tenant.id
    assert bills[0].shop_id == meter.shop_id
    assert bills[0].status == "pending"
    assert reading.bill_id == bills[0].id


def test_admin_reading_wins_over_customer_reading(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    """
    THE critical rule. Tenant says 12732, admin reads 12730 off the photo.
    The bill must be 280 units, not 282.
    """
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]

    resp = client.post(
        f"/api/meter-readings/{reading_id}/approve",
        json={"admin_verified_reading": 12730,
              "override_reason": "Photo clearly shows 12730."},
        headers=admin_auth,
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    reading = db.get(MeterReading, reading_id)
    assert reading.approved_reading == Decimal("12730.00")
    assert reading.calculated_units == Decimal("280.00")
    assert reading.override_reason == "Photo clearly shows 12730."
    # Tenant's original entry is preserved, not overwritten.
    assert reading.customer_reading == Decimal("12732.00")

    assert db.query(Bill).first().amount == Decimal("2660.00")   # 280 x 9.50


def test_override_without_a_reason_is_refused(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(f"/api/meter-readings/{reading_id}/approve",
                       json={"admin_verified_reading": 12730}, headers=admin_auth)
    assert resp.status_code == 400
    assert "reason" in resp.json()["detail"].lower()

    db.expire_all()
    assert db.get(MeterReading, reading_id).status == "pending"
    assert db.query(Bill).count() == 0


def test_double_approval_creates_only_one_bill(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    """Double-clicking Approve must not bill the tenant twice."""
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    payload = {"admin_verified_reading": 12732}

    first = client.post(f"/api/meter-readings/{reading_id}/approve", json=payload, headers=admin_auth)
    second = client.post(f"/api/meter-readings/{reading_id}/approve", json=payload, headers=admin_auth)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already been approved" in second.json()["detail"]

    db.expire_all()
    assert db.query(Bill).count() == 1
    assert db.query(MeterReading).filter(MeterReading.status == "approved").count() == 1


def test_approval_below_previous_reading_creates_no_bill(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(
        f"/api/meter-readings/{reading_id}/approve",
        json={"admin_verified_reading": 12000, "override_reason": "typo test"},
        headers=admin_auth,
    )
    assert resp.status_code == 400

    db.expire_all()
    assert db.query(Bill).count() == 0
    assert db.get(MeterReading, reading_id).status == "pending"


def test_approval_without_a_tariff_rolls_back_completely(
    client, tenant_auth, admin_auth, meter, photo_dir, db
):
    """No tariff configured -> no bill AND no half-approved reading."""
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(f"/api/meter-readings/{reading_id}/approve",
                       json={"admin_verified_reading": 12732}, headers=admin_auth)
    assert resp.status_code == 400
    assert "No unit price" in resp.json()["detail"]

    db.expire_all()
    reading = db.get(MeterReading, reading_id)
    assert reading.status == "pending"
    assert reading.approved_reading is None
    assert db.query(Bill).count() == 0


def test_historical_tariff_is_preserved_on_the_bill(
    client, tenant_auth, admin_auth, meter, photo_dir, db
):
    """Raising the rate later must not change an already-issued bill."""
    db.add(MeterTariff(meter_type="electricity", unit_price=Decimal("9.50"),
                       effective_from=datetime(2000, 1, 1)))
    db.commit()

    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    db.expire_all()
    original_amount = db.query(Bill).first().amount
    assert original_amount == Decimal("2679.00")

    # Price rises afterwards.
    db.add(MeterTariff(meter_type="electricity", unit_price=Decimal("12.00"),
                       effective_from=datetime.now()))
    db.commit()
    db.expire_all()

    assert db.query(Bill).first().amount == original_amount
    assert db.get(MeterReading, reading_id).unit_price_applied == Decimal("9.5000")


def test_approved_reading_becomes_previous_for_the_next_submission(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    first_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{first_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    second = _submit(client, tenant_auth, meter.id, 13000)
    assert second.json()["reading"]["previous_reading"] == 12732.0

    second_id = second.json()["reading"]["id"]
    client.post(f"/api/meter-readings/{second_id}/approve",
                json={"admin_verified_reading": 13000}, headers=admin_auth)

    db.expire_all()
    assert db.get(MeterReading, second_id).calculated_units == Decimal("268.00")
    assert db.query(Bill).count() == 2


def test_billing_can_be_switched_off_in_settings(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    import settings_service
    settings_service.set_many(db, {"meter.auto_create_bill": False})
    db.commit()
    settings_service.invalidate_cache()

    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(f"/api/meter-readings/{reading_id}/approve",
                       json={"admin_verified_reading": 12732}, headers=admin_auth)
    assert resp.status_code == 200
    assert resp.json()["result"]["bill_created"] is False

    db.expire_all()
    assert db.query(Bill).count() == 0
    assert db.get(MeterReading, reading_id).status == "approved"


# ══════════════════════════════════════════════════════════════════════════════
# REJECTION
# ══════════════════════════════════════════════════════════════════════════════

def test_reject_records_the_reason_and_creates_no_bill(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    resp = client.post(f"/api/meter-readings/{reading_id}/reject",
                       json={"reason": "Photo is too blurry to read."}, headers=admin_auth)
    assert resp.status_code == 200

    db.expire_all()
    reading = db.get(MeterReading, reading_id)
    assert reading.status == "rejected"
    assert reading.rejection_reason == "Photo is too blurry to read."
    assert db.query(Bill).count() == 0


def test_tenant_sees_the_rejection_reason(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/reject",
                json={"reason": "Wrong meter photographed."}, headers=admin_auth)

    body = client.get(f"/api/tenant/meter-readings/{reading_id}", headers=tenant_auth).json()
    assert body["status"] == "rejected"
    assert body["rejection_reason"] == "Wrong meter photographed."


def test_tenant_can_resubmit_after_rejection(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    first_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{first_id}/reject",
                json={"reason": "Blurry"}, headers=admin_auth)
    assert _submit(client, tenant_auth, meter.id, 12735).status_code == 201


def test_approved_reading_cannot_be_rejected(client, tenant_auth, admin_auth, meter, photo_dir, tariff):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    resp = client.post(f"/api/meter-readings/{reading_id}/reject",
                       json={"reason": "changed my mind"}, headers=admin_auth)
    assert resp.status_code == 409


def test_rejected_reading_cannot_be_approved(client, tenant_auth, admin_auth, meter, photo_dir, tariff, db):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/reject",
                json={"reason": "Blurry"}, headers=admin_auth)

    resp = client.post(f"/api/meter-readings/{reading_id}/approve",
                       json={"admin_verified_reading": 12732}, headers=admin_auth)
    assert resp.status_code == 409
    assert db.query(Bill).count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT TRAIL
# ══════════════════════════════════════════════════════════════════════════════

def test_the_whole_workflow_is_audited(client, tenant_auth, admin_auth, meter, photo_dir, tariff, db):
    from create_tables import AuditLog

    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12730, "override_reason": "Photo shows 12730"},
                headers=admin_auth)

    actions = [a.action for a in db.query(AuditLog).all()]
    assert "SUBMIT" in actions      # tenant uploaded
    assert "APPROVE" in actions     # admin approved
    assert "CREATE" in actions      # bill raised

    approve_log = db.query(AuditLog).filter(AuditLog.action == "APPROVE").first()
    assert "Photo shows 12730" in approve_log.new_data
    assert '"override": true' in approve_log.new_data.lower()


# ══════════════════════════════════════════════════════════════════════════════
# METER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def test_admin_can_create_and_list_meters(client, admin_auth, shop):
    resp = client.post("/api/meters", headers=admin_auth, json={
        "shop_id": shop.id, "meter_number": "MTR-NEW", "initial_reading": 500,
    })
    assert resp.status_code == 201
    assert resp.json()["current_previous_reading"] == 500.0
    assert len(client.get("/api/meters", headers=admin_auth).json()) == 1


def test_duplicate_meter_number_on_same_shop_is_refused(client, admin_auth, shop, meter):
    resp = client.post("/api/meters", headers=admin_auth,
                       json={"shop_id": shop.id, "meter_number": meter.meter_number})
    assert resp.status_code == 409


def test_initial_reading_locked_once_approved_history_exists(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    resp = client.put(f"/api/meters/{meter.id}", json={"initial_reading": 1},
                      headers=admin_auth)
    assert resp.status_code == 400
    assert "cannot be changed" in resp.json()["detail"]


def test_meter_with_approved_readings_cannot_be_deleted(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    resp = client.delete(f"/api/meters/{meter.id}", headers=admin_auth)
    assert resp.status_code == 400
    assert "cannot be deleted" in resp.json()["detail"]


def test_tenant_only_sees_meters_on_their_own_shops(
    client, tenant_auth, other_tenant_auth, meter
):
    mine = client.get("/api/tenant/meters", headers=tenant_auth).json()
    assert len(mine) == 1
    assert mine[0]["meter_number"] == "MTR-001"
    assert mine[0]["previous_reading"] == 12450.0

    assert client.get("/api/tenant/meters", headers=other_tenant_auth).json() == []


# ══════════════════════════════════════════════════════════════════════════════
# TARIFF API
# ══════════════════════════════════════════════════════════════════════════════

def test_admin_can_add_a_tariff_and_history_is_kept(client, admin_auth):
    for price, date in (("8.00", "2026-04-01T00:00:00"), ("9.50", "2026-07-01T00:00:00")):
        resp = client.post("/api/meter-tariffs", headers=admin_auth, json={
            "meter_type": "electricity", "unit_price": float(price), "effective_from": date,
        })
        assert resp.status_code == 201

    body = client.get("/api/meter-tariffs", headers=admin_auth).json()
    assert len(body["tariffs"]) == 2
    assert body["tariffs"][0]["unit_price"] == 9.5     # newest first


def test_tariff_used_by_a_bill_cannot_be_deleted(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff
):
    reading_id = _submit(client, tenant_auth, meter.id, 12732).json()["reading"]["id"]
    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    resp = client.delete(f"/api/meter-tariffs/{tariff.id}", headers=admin_auth)
    assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def test_admin_can_read_and_change_settings(client, admin_auth):
    body = client.get("/api/settings", headers=admin_auth).json()
    assert any(s["key"] == "app.name" for s in body["settings"])

    resp = client.put("/api/settings", headers=admin_auth,
                      json={"values": {"app.name": "Gund Properties"}})
    assert resp.status_code == 200
    assert resp.json()["changed"]["app.name"]["new"] == "Gund Properties"

    assert client.get("/api/settings/public").json()["app_name"] == "Gund Properties"


def test_invalid_setting_value_is_refused_and_nothing_is_saved(client, admin_auth):
    resp = client.put("/api/settings", headers=admin_auth,
                      json={"values": {"app.name": "Valid Name", "meter.photo_max_mb": 999}})
    assert resp.status_code == 400

    # The whole batch was rejected - the valid key in it must not have applied.
    assert client.get("/api/settings/public").json()["app_name"] != "Valid Name"


def test_unknown_setting_key_is_refused(client, admin_auth):
    resp = client.put("/api/settings", headers=admin_auth,
                      json={"values": {"totally.made.up": "x"}})
    assert resp.status_code == 400


def test_settings_can_be_reset_to_defaults(client, admin_auth):
    client.put("/api/settings", headers=admin_auth, json={"values": {"app.name": "Temp"}})
    client.post("/api/settings/reset", headers=admin_auth)
    assert client.get("/api/settings/public").json()["app_name"] == "Ledger"


def test_tenant_cannot_change_settings(client, tenant_auth):
    assert client.get("/api/settings", headers=tenant_auth).status_code == 403
    assert client.put("/api/settings", headers=tenant_auth,
                      json={"values": {"app.name": "Hacked"}}).status_code == 403


def test_public_settings_do_not_leak_internal_config(client):
    """The unauthenticated endpoint must expose only display values."""
    body = client.get("/api/settings/public").json()
    assert set(body.keys()) == {"app_name", "tagline", "currency_symbol",
                                "support_contact", "labels"}
    assert "meter.photo_storage_dir" not in str(body)
