"""
Regression guard for the pre-existing application.

The submeter feature added models, imports and routes to app.py. These tests
prove the parts that were already working still work: auth, roles, the
complex/shop/user CRUD, bills, payments and the reports the admin UI depends on.
"""

from datetime import datetime
from decimal import Decimal

from create_tables import Bill, Payment, Shop, User, UserShop


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
# RENT BILL GENERATION (the existing scheduler's logic)
# ══════════════════════════════════════════════════════════════════════════════

def test_auto_rent_generation_is_idempotent(client, admin_auth, db, tenant, shop):
    """Running generation twice for the same day must not double-bill."""
    tenant.auto_rent_bill_enabled = True
    tenant.rent_bill_date = 5
    db.commit()

    target = "2026-06-05"
    first = client.post(f"/api/bills/generate-rent?date={target}", headers=admin_auth)
    assert first.status_code == 200
    assert len(first.json()["created"]) == 1

    second = client.post(f"/api/bills/generate-rent?date={target}", headers=admin_auth)
    assert second.json()["created"] == []
    assert second.json()["skipped_existing"] == 1

    assert db.query(Bill).filter(Bill.bill_type == "Rent").count() == 1


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
