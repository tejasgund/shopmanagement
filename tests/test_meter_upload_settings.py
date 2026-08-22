"""
The Meter Reading image-upload switches and the tenant submission window.

These gate the EXISTING reading/photo flow rather than adding a second one, so
what is asserted here is mostly what must NOT change: the reading itself stays
submittable however the switches are set, and each switch touches only its own
role. The acceptance criteria they came from are quoted at each section.
"""

from datetime import date

import pytest

from conftest import make_jpeg
from create_tables import Meter, MeterReading
import settings_service


def _set(db, **values):
    settings_service.set_many(db, values)
    db.commit()
    settings_service.invalidate_cache()


def _submit_tenant(client, auth, meter_id, reading, photo=True):
    """Exactly what the tenant portal sends: multipart, photo optional."""
    files = {"photo": ("meter.jpg", make_jpeg(), "image/jpeg")} if photo else None
    return client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter_id), "customer_reading": str(reading)},
        files=files, headers=auth,
    )


def _collect_admin(client, auth, meter_id, reading, photo=True):
    """The admin 'collect on the tenant's behalf' form."""
    files = {"photo": ("meter.jpg", make_jpeg(), "image/jpeg")} if photo else None
    return client.post(
        "/api/meter-readings/collect",
        data={"meter_id": str(meter_id), "customer_reading": str(reading)},
        files=files, headers=auth,
    )


# ══════════════════════════════════════════════════════════════════════════════
# "Disabling image upload must not prevent meter reading submission."
# ══════════════════════════════════════════════════════════════════════════════

def test_tenant_can_still_submit_a_reading_when_photo_upload_is_off(
    client, tenant_auth, meter, db, photo_dir
):
    _set(db, **{"meter.allow_tenant_photo_upload": False})
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 201, resp.text
    assert resp.json()["reading"]["customer_reading"] == 12732.0
    assert resp.json()["reading"]["has_photo"] is False


def test_photo_required_is_ignored_while_tenant_upload_is_off(
    client, tenant_auth, meter, db, photo_dir
):
    """
    The two settings contradict each other: one demands a photo, the other
    removes any way to send one. The reading must win - otherwise turning off
    uploads silently locks tenants out of submitting at all.
    """
    _set(db, **{
        "meter.photo_required": True,
        "meter.allow_tenant_photo_upload": False,
    })
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 201, resp.text


def test_admin_can_still_collect_a_reading_when_photo_upload_is_off(
    client, admin_auth, meter, db, photo_dir
):
    _set(db, **{
        "meter.photo_required": True,
        "meter.allow_admin_photo_upload": False,
    })
    resp = _collect_admin(client, admin_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 201, resp.text


# ══════════════════════════════════════════════════════════════════════════════
# A photo sent while the switch is off is refused rather than silently dropped
# ══════════════════════════════════════════════════════════════════════════════

def test_tenant_photo_is_refused_not_silently_discarded(
    client, tenant_auth, meter, db, photo_dir
):
    _set(db, **{"meter.allow_tenant_photo_upload": False})
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=True)
    assert resp.status_code == 400
    assert "turned off" in resp.json()["detail"].lower()
    # and nothing was written, so the tenant can simply resend without it
    assert db.query(MeterReading).count() == 0


def test_admin_photo_is_refused_not_silently_discarded(
    client, admin_auth, meter, db, photo_dir
):
    _set(db, **{"meter.allow_admin_photo_upload": False})
    resp = _collect_admin(client, admin_auth, meter.id, 12732, photo=True)
    assert resp.status_code == 400
    assert db.query(MeterReading).count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# "Admin setting controls only admin uploads. Tenant setting only tenant."
# ══════════════════════════════════════════════════════════════════════════════

def test_turning_off_tenant_upload_leaves_admin_upload_working(
    client, admin_auth, meter, db, photo_dir
):
    _set(db, **{
        "meter.allow_tenant_photo_upload": False,
        "meter.allow_admin_photo_upload": True,
    })
    resp = _collect_admin(client, admin_auth, meter.id, 12732, photo=True)
    assert resp.status_code == 201, resp.text
    assert db.query(MeterReading).first().photo_path is not None


def test_turning_off_admin_upload_leaves_tenant_upload_working(
    client, tenant_auth, meter, db, photo_dir
):
    _set(db, **{
        "meter.allow_admin_photo_upload": False,
        "meter.allow_tenant_photo_upload": True,
    })
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=True)
    assert resp.status_code == 201, resp.text
    assert db.query(MeterReading).first().photo_path is not None


def test_both_switches_on_is_the_existing_behaviour(
    client, tenant_auth, meter, db, photo_dir
):
    """Defaults must leave the feature exactly as it was before these settings."""
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=True)
    assert resp.status_code == 201, resp.text
    assert resp.json()["reading"]["has_photo"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Tenant submission window (day-of-month, repeats every month)
# ══════════════════════════════════════════════════════════════════════════════

def test_tenant_is_blocked_outside_the_window(
    client, tenant_auth, meter, db, photo_dir
):
    # A window that deliberately excludes today.
    today = date.today().day
    if today <= 27:
        start, end = today + 1, 28
    else:
        start, end = 1, today - 1
    _set(db, **{
        "meter.tenant_upload_any_day": False,
        "meter.tenant_upload_from_day": start,
        "meter.tenant_upload_to_day": end,
    })
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 403, resp.text
    assert "day" in resp.json()["detail"].lower()
    assert db.query(MeterReading).count() == 0


def test_tenant_can_submit_inside_the_window(
    client, tenant_auth, meter, db, photo_dir
):
    _set(db, **{
        "meter.tenant_upload_any_day": False,
        "meter.tenant_upload_from_day": 1,
        "meter.tenant_upload_to_day": 31,      # every day of any month
        "meter.photo_required": False,
    })
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 201, resp.text


def test_allow_every_day_overrides_the_window(
    client, tenant_auth, meter, db, photo_dir
):
    today = date.today().day
    start, end = (today + 1, 28) if today <= 27 else (1, today - 1)
    _set(db, **{
        "meter.tenant_upload_any_day": True,   # the override
        "meter.tenant_upload_from_day": start, # window that excludes today
        "meter.tenant_upload_to_day": end,
        "meter.photo_required": False,
    })
    resp = _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 201, resp.text


def test_admin_is_never_restricted_by_the_tenant_window(
    client, admin_auth, meter, db, photo_dir
):
    """"Admin should always be able to submit regardless of the tenant window."""
    today = date.today().day
    start, end = (today + 1, 28) if today <= 27 else (1, today - 1)
    _set(db, **{
        "meter.tenant_upload_any_day": False,
        "meter.tenant_upload_from_day": start,
        "meter.tenant_upload_to_day": end,
        "meter.photo_required": False,
    })
    resp = _collect_admin(client, admin_auth, meter.id, 12732, photo=False)
    assert resp.status_code == 201, resp.text


def test_the_portal_is_told_the_window_state(client, tenant_auth, meter, db):
    """The tenant app hides the button using these, so they must be present."""
    _set(db, **{
        "meter.tenant_upload_any_day": False,
        "meter.tenant_upload_from_day": 3,
        "meter.tenant_upload_to_day": 9,
        "meter.allow_tenant_photo_upload": False,
    })
    cfg = client.get("/api/tenant/home", headers=tenant_auth).json()["settings"]
    assert cfg["meter_photo_upload_enabled"] is False
    assert cfg["meter_photo_required"] is False        # yields to the switch
    assert cfg["meter_upload_window"] == {"any_day": False, "from_day": 3, "to_day": 9}
    assert cfg["meter_upload_open_today"] is (3 <= date.today().day <= 9)


# ══════════════════════════════════════════════════════════════════════════════
# Settings validation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [0, 32, -1])
def test_window_days_outside_1_to_31_are_rejected(db, bad):
    with pytest.raises(ValueError, match="between 1 and 31"):
        settings_service.set_many(db, {"meter.tenant_upload_from_day": bad})


def test_a_from_day_after_the_to_day_is_rejected(db):
    """Left alone this makes a window no day satisfies - a silent lockout."""
    with pytest.raises(ValueError, match="cannot be after"):
        settings_service.set_many(db, {
            "meter.tenant_upload_from_day": 20,
            "meter.tenant_upload_to_day": 5,
        })


def test_saving_one_day_is_validated_against_the_stored_other_one(db):
    """Half a pair must still be checked against what is already saved."""
    _set(db, **{
        "meter.tenant_upload_from_day": 5,
        "meter.tenant_upload_to_day": 20,
    })
    with pytest.raises(ValueError, match="cannot be after"):
        settings_service.set_many(db, {"meter.tenant_upload_from_day": 25})


# ══════════════════════════════════════════════════════════════════════════════
# Missing-readings report
# "based on meter reading submission, not whether the tenant uploaded an image"
# ══════════════════════════════════════════════════════════════════════════════

def _report(client, admin_auth, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(f"/api/reports/missing-meter-readings?{qs}", headers=admin_auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_report_lists_a_tenant_who_has_not_submitted(
    client, admin_auth, meter, tenant, shop, db
):
    rep = _report(client, admin_auth, scope="month")
    assert rep["summary"] == {"total": 1, "submitted": 0, "not_submitted": 1}
    row = rep["rows"][0]
    assert row["tenant_name"] == tenant.name
    assert row["shop_number"] == shop.shop_number
    assert row["meter_number"] == meter.meter_number
    assert row["status"] == "Not Submitted"
    assert row["submitted_at"] is None
    assert row["reading_date"] is None


def test_a_reading_with_no_photo_still_counts_as_submitted(
    client, admin_auth, tenant_auth, meter, db, photo_dir
):
    """The whole point: the report tracks readings, not photos."""
    _set(db, **{
        "meter.allow_tenant_photo_upload": False,   # so no photo is even possible
    })
    assert _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False).status_code == 201

    rep = _report(client, admin_auth, scope="month")
    row = rep["rows"][0]
    assert row["status"] == "Submitted"
    assert row["has_photo"] is False
    assert row["submitted_at"] is not None
    assert rep["summary"]["not_submitted"] == 0


def test_report_day_scope_is_narrower_than_month_scope(
    client, admin_auth, tenant_auth, meter, db, photo_dir
):
    _set(db, **{"meter.photo_required": False})
    assert _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False).status_code == 201

    today = date.today()
    # Submitted today -> present under both scopes for today...
    assert _report(client, admin_auth, scope="day", date=today.isoformat())["summary"]["submitted"] == 1
    assert _report(client, admin_auth, scope="month", date=today.isoformat())["summary"]["submitted"] == 1

    # ...but a different day in the same month is "not submitted" by day,
    # while the month view still counts it.
    other_day = today.replace(day=1) if today.day != 1 else today.replace(day=2)
    by_day = _report(client, admin_auth, scope="day", date=other_day.isoformat())
    by_month = _report(client, admin_auth, scope="month", date=other_day.isoformat())
    assert by_day["summary"]["submitted"] == 0
    assert by_month["summary"]["submitted"] == 1


def test_report_counts_a_rejected_reading_as_submitted(
    client, admin_auth, tenant_auth, meter, db, photo_dir
):
    """They did send it - the review outcome is shown separately."""
    _set(db, **{"meter.photo_required": False})
    _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)
    reading = db.query(MeterReading).first()
    reading.status = "rejected"
    db.commit()

    row = _report(client, admin_auth, scope="month")["rows"][0]
    assert row["status"] == "Submitted"
    assert row["reading_status"] == "rejected"


def test_report_ignores_meters_with_no_shop_or_no_longer_in_use(
    client, admin_auth, meter, db
):
    """Nobody could have been expected to read these."""
    db.add(Meter(shop_id=None, meter_number="SPARE-1", meter_type="electricity",
                 initial_reading=0, is_active=True))
    db.add(Meter(shop_id=meter.shop_id, meter_number="OLD-1", meter_type="electricity",
                 initial_reading=0, is_active=False))
    db.commit()

    rep = _report(client, admin_auth, scope="month")
    assert [r["meter_number"] for r in rep["rows"]] == [meter.meter_number]


def test_report_status_filter_does_not_change_the_totals(
    client, admin_auth, tenant_auth, meter, db, photo_dir
):
    """The summary describes the period; the filter only narrows the rows."""
    _set(db, **{"meter.photo_required": False})
    _submit_tenant(client, tenant_auth, meter.id, 12732, photo=False)

    rep = _report(client, admin_auth, scope="month", status="not_submitted")
    assert rep["summary"]["submitted"] == 1      # unchanged by the filter
    assert rep["rows"] == []


def test_report_is_admin_only(client, tenant_auth):
    resp = client.get("/api/reports/missing-meter-readings", headers=tenant_auth)
    assert resp.status_code == 403
