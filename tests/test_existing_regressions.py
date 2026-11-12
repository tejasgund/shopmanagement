"""
Regression guard for the pre-existing application.

The submeter feature added models, imports and routes to app.py. These tests
prove the parts that were already working still work: auth, roles, the
complex/shop/user CRUD, bills, payments and the reports the admin UI depends on.
"""

from datetime import datetime
from decimal import Decimal

from models.schema import Bill, Payment, Shop, User, UserShop
from services import settings as settings_service
# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

def test_login_succeeds_with_correct_credentials(client, admin):
    resp = client.post("/api/login", json={"mobile": admin.mobile, "password": "admin123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["role"] == "admin"
    assert body["token"]


def test_login_fails_with_wrong_password(client, admin):
    resp = client.post("/api/login", json={"mobile": admin.mobile, "password": "wrong"})
    assert resp.status_code == 401


def test_protected_route_needs_a_token(client):
    assert client.get("/api/complex").status_code in (401, 403)


def test_tenant_cannot_reach_admin_routes(client, tenant_auth):
    assert client.get("/api/complex", headers=tenant_auth).status_code == 403
    assert client.get("/api/user", headers=tenant_auth).status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# CORE CRUD
# ══════════════════════════════════════════════════════════════════════════════

def test_complex_create_and_list(client, admin_auth):
    resp = client.post("/api/complex", headers=admin_auth,
                       json={"name": "Sunrise Plaza", "address": "MG Road"})
    assert resp.status_code in (200, 201)
    assert any(c["name"] == "Sunrise Plaza"
               for c in client.get("/api/complex", headers=admin_auth).json())


def test_shop_create_and_list(client, admin_auth):
    resp = client.post("/api/shop", headers=admin_auth,
                       json={"shop_number": "C-303", "shop_rent": 12000, "shop_deposit": 60000})
    assert resp.status_code in (200, 201)
    assert any(s["shop_number"] == "C-303"
               for s in client.get("/api/shop", headers=admin_auth).json())


def test_user_create_and_list(client, admin_auth):
    resp = client.post("/api/user", headers=admin_auth, json={
        "name": "New Tenant", "mobile": "9111111111",
        "password": "secret123", "role": "tenant",
    })
    assert resp.status_code in (200, 201)
    assert any(u["mobile"] == "9111111111"
               for u in client.get("/api/user", headers=admin_auth).json())


# ══════════════════════════════════════════════════════════════════════════════
# BILLS AND PAYMENTS
# ══════════════════════════════════════════════════════════════════════════════

def test_rent_bill_amount_comes_from_the_shop_not_the_request(client, admin_auth, tenant, shop):
    """
    Documented existing behaviour: for bill_type "Rent" the server ignores any
    amount in the request and uses the shop's current rent, so rent can never
    drift from what is configured on the shop.
    """
    resp = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id, "bill_type": "Rent",
        "amount": 1,                                  # deliberately wrong
        "bill_date": datetime(2026, 6, 1).isoformat(),
    })
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["amount"] == 10000.0           # the shop's rent


def test_due_date_defaults_to_bill_date_plus_configured_due_days(
    client, admin_auth, tenant, shop, db
):
    """
    A bill submitted without a due date gets one computed from the admin's
    "Default bill due period (days)" setting (bill.due_days): due date =
    bill date + that many days.

    Regression guard. This calculation was silently lost when create_bill was
    moved out of app.py into routers/bills.py - the extracted copy just passed
    the request's due_date straight through, so every bill created without an
    explicit due date got NULL and the setting appeared to do nothing however
    it was changed. Nothing covered it, so nothing caught it.
    """
    settings_service.set_many(db, {"bill.due_days": 10})
    db.commit()
    settings_service.invalidate_cache()

    resp = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id, "bill_type": "Rent",
        "bill_date": datetime(2026, 6, 1).isoformat(),
        "due_date": None,                              # what the UI sends when left blank
    })
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["due_date"].startswith("2026-06-11")   # 1 June + 10 days


def test_changing_the_due_days_setting_changes_the_next_bills_due_date(
    client, admin_auth, tenant, shop, db
):
    """The setting is read per request, not frozen at import time."""
    settings_service.set_many(db, {"bill.due_days": 7})
    db.commit()
    settings_service.invalidate_cache()
    first = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id, "bill_type": "Rent",
        "bill_date": datetime(2026, 6, 1).isoformat(),
    })
    assert first.json()["due_date"].startswith("2026-06-08")

    settings_service.set_many(db, {"bill.due_days": 45})
    db.commit()
    settings_service.invalidate_cache()
    second = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id, "bill_type": "Electricity",
        "amount": 500,
        "bill_date": datetime(2026, 6, 1).isoformat(),
    })
    assert second.json()["due_date"].startswith("2026-07-16")  # 1 June + 45 days


def test_an_explicit_due_date_still_overrides_the_setting(
    client, admin_auth, tenant, shop, db
):
    """The setting is only a default - an admin can always set a date by hand."""
    settings_service.set_many(db, {"bill.due_days": 10})
    db.commit()
    settings_service.invalidate_cache()

    resp = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id, "bill_type": "Rent",
        "bill_date": datetime(2026, 6, 1).isoformat(),
        "due_date": datetime(2026, 12, 25).isoformat(),
    })
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["due_date"].startswith("2026-12-25")


def test_bill_create_and_payment_reconciliation(client, admin_auth, tenant, shop, db):
    """A rent bill plus a part-payment should leave the bill 'partial'."""
    bill_id = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id, "bill_type": "Rent",
        "bill_date": datetime(2026, 6, 1).isoformat(),
    }).json()["id"]                                   # amount = shop rent = 10000

    pay = client.post("/api/payment", headers=admin_auth, json={
        "bill_id": bill_id, "amount": 4000, "payment_method": "Cash",
    })
    assert pay.status_code in (200, 201), pay.text

    db.expire_all()
    bill = db.get(Bill, bill_id)
    assert bill.paid_amount == Decimal("4000.00")
    assert bill.pending_amount == Decimal("6000.00")
    assert bill.status == "partial"


def test_full_payment_marks_bill_paid(client, admin_auth, tenant, shop, db):
    # Non-rent bill, so the amount posted is used as-is.
    bill_id = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id,
        "bill_type": "Maintenance", "amount": 5000,
    }).json()["id"]

    client.post("/api/payment", headers=admin_auth,
                json={"bill_id": bill_id, "amount": 5000, "payment_method": "UPI"})

    db.expire_all()
    bill = db.get(Bill, bill_id)
    assert bill.pending_amount == Decimal("0.00")
    assert bill.status == "paid"


def test_paying_an_already_paid_bill_is_refused(client, admin_auth, tenant, shop):
    bill_id = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id,
        "bill_type": "Maintenance", "amount": 1000,
    }).json()["id"]
    client.post("/api/payment", headers=admin_auth,
                json={"bill_id": bill_id, "amount": 1000, "payment_method": "Cash"})

    again = client.post("/api/payment", headers=admin_auth,
                        json={"bill_id": bill_id, "amount": 500, "payment_method": "Cash"})
    assert again.status_code == 400


def test_bill_list_endpoints_still_respond(client, admin_auth):
    assert client.get("/api/bill", headers=admin_auth).status_code == 200
    assert client.get("/api/payment", headers=admin_auth).status_code == 200
    assert client.get("/api/bills", headers=admin_auth).status_code == 200
    assert client.get("/api/payments", headers=admin_auth).status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS / DASHBOARD (the admin UI depends on every one of these)
# ══════════════════════════════════════════════════════════════════════════════

def test_reports_endpoints_still_respond(client, admin_auth):
    for path in (
        "/api/reports/summary",
        "/api/reports/business-overview",
        "/api/reports/occupancy",
        "/api/reports/deposit",
        "/api/reports/rent-collection",
        "/api/reports/user-wise",
        "/api/dashboard/kpis",
        "/api/finance/overview",
    ):
        assert client.get(path, headers=admin_auth).status_code == 200, path


def test_audit_log_endpoint_still_responds(client, admin_auth):
    assert client.get("/api/audit-logs", headers=admin_auth).status_code == 200


def test_global_search_still_responds(client, admin_auth):
    assert client.get("/api/search?q=test", headers=admin_auth).status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# TENANT PORTAL
# ══════════════════════════════════════════════════════════════════════════════

def test_tenant_portal_endpoints_still_respond(client, tenant_auth):
    for path in ("/api/tenant/profile", "/api/tenant/shops", "/api/tenant/bills",
                 "/api/tenant/payments", "/api/tenant/financial-summary"):
        assert client.get(path, headers=tenant_auth).status_code == 200, path


def test_tenant_only_sees_their_own_bills(client, admin_auth, tenant_auth,
                                          tenant, other_tenant, shop, other_shop):
    mine = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id,
        "bill_type": "Maintenance", "amount": 1000,
    }).json()["id"]
    theirs = client.post("/api/bill", headers=admin_auth, json={
        "user_id": other_tenant.id, "shop_id": other_shop.id,
        "bill_type": "Maintenance", "amount": 2000,
    }).json()["id"]

    visible = {b["id"] for b in client.get("/api/tenant/bills", headers=tenant_auth).json()}
    assert mine in visible
    assert theirs not in visible


# ══════════════════════════════════════════════════════════════════════════════
# DUPLICATE RENT PROTECTION
#
# Rent generation itself now lives in a standalone cron script
# (scheduler/auto_rent_generation/) which is not importable from here by
# design. What the APPLICATION still has to guarantee is that it cannot create
# a second Rent bill for a tenant/shop/month either - because the admin's
# manual bill screen is the other way one can come into existence.
#
# The guarantee is the UNIQUE index on (user_id, shop_id, rent_period), not a
# check in a code path, so it holds however the second bill is attempted.
# ══════════════════════════════════════════════════════════════════════════════

def test_a_manually_created_rent_bill_is_stamped_with_its_month(
    client, admin_auth, db, tenant, shop,
):
    """The stamp is what the unique index protects; an unstamped bill is
    unprotected, so this is the precondition for everything below."""
    res = client.post("/api/bill", headers=admin_auth, json={
        "user_id": tenant.id, "shop_id": shop.id,
        "bill_type": "Rent", "bill_date": "2026-06-05T00:00:00",
    })
    assert res.status_code == 201, res.text

    bill = db.query(Bill).filter(Bill.id == res.json()["id"]).one()
    assert bill.rent_period == "RENT-2026-06"


def test_a_second_rent_bill_for_the_same_month_is_refused(
    client, admin_auth, db, tenant, shop,
):
    """The regression this exists for: shop 10 receiving two identical rent
    bills on 2026-08-13. Now impossible at the database, not merely unlikely."""
    payload = {
        "user_id": tenant.id, "shop_id": shop.id,
        "bill_type": "Rent", "bill_date": "2026-06-05T00:00:00",
    }
    first = client.post("/api/bill", headers=admin_auth, json=payload)
    assert first.status_code == 201, first.text

    # Same month, different day - still the same rent period.
    payload["bill_date"] = "2026-06-20T00:00:00"
    second = client.post("/api/bill", headers=admin_auth, json=payload)

    assert second.status_code == 400
    assert "already exists" in second.json()["detail"]
    assert db.query(Bill).filter(Bill.bill_type == "Rent").count() == 1


def test_rent_bills_in_different_months_are_both_allowed(
    client, admin_auth, db, tenant, shop,
):
    """The constraint must bite on duplicates only - a tenant is billed every
    month, and June and July are not duplicates of each other."""
    for bill_date in ("2026-06-05T00:00:00", "2026-07-05T00:00:00"):
        res = client.post("/api/bill", headers=admin_auth, json={
            "user_id": tenant.id, "shop_id": shop.id,
            "bill_type": "Rent", "bill_date": bill_date,
        })
        assert res.status_code == 201, res.text

    assert db.query(Bill).filter(Bill.bill_type == "Rent").count() == 2


def test_non_rent_bills_are_not_constrained(client, admin_auth, db, tenant, shop):
    """Several electricity or maintenance charges in one month are ordinary,
    so they carry no rent period and the index leaves them alone."""
    for _ in range(3):
        res = client.post("/api/bill", headers=admin_auth, json={
            "user_id": tenant.id, "shop_id": shop.id,
            "bill_type": "Maintenance", "amount": 500,
            "bill_date": "2026-06-05T00:00:00",
        })
        assert res.status_code == 201, res.text

    bills = db.query(Bill).filter(Bill.bill_type == "Maintenance").all()
    assert len(bills) == 3
    assert all(b.rent_period is None for b in bills)


# ══════════════════════════════════════════════════════════════════════════════
# THE NEW FEATURE MUST NOT DISTURB EXISTING BILLING
# ══════════════════════════════════════════════════════════════════════════════

def test_meter_bills_appear_in_the_normal_bill_list(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff
):
    """A bill raised from a reading is an ordinary bill - visible everywhere."""
    from conftest import make_jpeg

    reading_id = client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter.id), "customer_reading": "12732"},
        files={"photo": ("m.jpg", make_jpeg(), "image/jpeg")},
        headers=tenant_auth,
    ).json()["reading"]["id"]

    client.post(f"/api/meter-readings/{reading_id}/approve",
                json={"admin_verified_reading": 12732}, headers=admin_auth)

    admin_bills = client.get("/api/bill", headers=admin_auth).json()
    assert any(b["bill_type"] == "Electricity" for b in admin_bills)

    tenant_bills = client.get("/api/tenant/bills", headers=tenant_auth).json()
    assert any(b["bill_type"] == "Electricity" for b in tenant_bills)


def test_meter_bill_can_be_paid_through_the_existing_payment_flow(
    client, tenant_auth, admin_auth, meter, photo_dir, tariff, db
):
    from conftest import make_jpeg

    reading_id = client.post(
        "/api/tenant/meter-readings",
        data={"meter_id": str(meter.id), "customer_reading": "12732"},
        files={"photo": ("m.jpg", make_jpeg(), "image/jpeg")},
        headers=tenant_auth,
    ).json()["reading"]["id"]

    approve = client.post(f"/api/meter-readings/{reading_id}/approve",
                          json={"admin_verified_reading": 12732}, headers=admin_auth)
    bill_id = approve.json()["result"]["bill_id"]

    pay = client.post("/api/payment", headers=admin_auth,
                      json={"bill_id": bill_id, "amount": 2679, "payment_method": "Cash"})
    assert pay.status_code in (200, 201)

    db.expire_all()
    assert db.get(Bill, bill_id).status == "paid"


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS CACHE FRESHNESS ACROSS WORKERS
# ══════════════════════════════════════════════════════════════════════════════

def test_settings_cache_expires_so_other_workers_pick_up_a_change(db, monkeypatch):
    """
    The API runs under `uvicorn --workers 2`. invalidate_cache() only reaches
    the process that handled the write, so without an expiry the OTHER worker
    serves its startup snapshot forever - an admin changes the Razorpay keys
    or the bill due period and half of all requests keep using the old value.

    Simulated here by writing to the database behind the cache's back (exactly
    what the other worker sees) and checking the value is picked up once the
    TTL lapses.
    """
    import time as _time
    from models.schema import AppSetting

    settings_service.set_many(db, {"bill.due_days": 10})
    db.commit()
    settings_service.invalidate_cache()
    assert settings_service.get_all(db)["bill.due_days"] == 10

    # Another worker writes the new value; this process is never told.
    row = db.query(AppSetting).filter(AppSetting.key == "bill.due_days").first()
    row.value = "45"
    db.commit()

    # Still the cached value - correct, the TTL hasn't lapsed.
    assert settings_service.get_all(db)["bill.due_days"] == 10

    monkeypatch.setattr(settings_service, "_CACHE_TTL_SECONDS", 0.0)
    assert settings_service.get_all(db)["bill.due_days"] == 45, (
        "a stale worker never picked up the change - the cache has no expiry"
    )


def test_settings_cache_still_updates_immediately_in_the_writing_process(db):
    """The TTL must not have made same-process writes slower to take effect."""
    settings_service.set_many(db, {"bill.due_days": 7})
    db.commit()
    settings_service.invalidate_cache()
    assert settings_service.get_all(db)["bill.due_days"] == 7
