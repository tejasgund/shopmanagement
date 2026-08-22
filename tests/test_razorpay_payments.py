"""
Tests for the Razorpay Standard Checkout integration (tenant online
payments): POST /api/tenant/payments/razorpay/create-order and
POST /api/tenant/payments/razorpay/verify.

The rules these lock down, in priority order:
  1. A Payment is only ever created AFTER a valid signature is verified -
     a signature mismatch must never mark anything as paid.
  2. The amount charged is decided server-side at create-order time (capped
     at the bill's pending balance) and never trusted from the client again
     at verify time.
  3. A tenant can only create an order / verify a payment for their OWN bill.
  4. The same order can never be verified twice (no double-crediting).
  5. The feature is OFF by default and stays off if the admin hasn't
     enabled it, even with valid keys present.
"""
import razorpay
import razorpay.errors
import pytest

from create_tables import Bill, Payment, RazorpayOrder
import settings_service


# ── Helpers ──────────────────────────────────────────────────────────────

def _enable_razorpay(db):
    settings_service.set_many(db, {"payment.razorpay_enabled": True})
    db.commit()
    settings_service.invalidate_cache()


@pytest.fixture
def bill(db, tenant, shop):
    b = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Rent",
              amount=1000, paid_amount=0, pending_amount=1000, status="pending")
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@pytest.fixture
def mock_order_create(monkeypatch):
    """Fakes Razorpay's order-creation API - returns an order echoing back
    whatever amount/currency/receipt was requested, like the real API does."""
    calls = []

    def fake_create(self, data=None, **kwargs):
        calls.append(data)
        return {
            "id": f"order_TEST{len(calls)}",
            "amount": data["amount"],
            "currency": data["currency"],
            "receipt": data.get("receipt"),
            "status": "created",
        }

    monkeypatch.setattr(razorpay.resources.order.Order, "create", fake_create)
    return calls


def _mock_verify_ok(monkeypatch):
    monkeypatch.setattr(
        razorpay.utility.utility.Utility, "verify_payment_signature",
        lambda self, parameters: True,
    )


def _mock_verify_fails(monkeypatch):
    def fail(self, parameters):
        raise razorpay.errors.SignatureVerificationError("Signature verification failed")
    monkeypatch.setattr(razorpay.utility.utility.Utility, "verify_payment_signature", fail)


# ══════════════════════════════════════════════════════════════════════════════
# CREATE ORDER
# ══════════════════════════════════════════════════════════════════════════════

def test_create_order_off_by_default(client, tenant_auth, bill, db):
    """payment.razorpay_enabled defaults False - the feature must be opt-in."""
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=tenant_auth)
    assert res.status_code == 403


def test_create_order_full_pending_amount(client, tenant_auth, bill, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=tenant_auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["amount"] == 100000          # Rs 1000 -> 100000 paise
    assert body["currency"] == "INR"
    assert body["order_id"] == "order_TEST1"
    assert body["key_id"]                     # public key handed to checkout.js, never the secret
    assert "key_secret" not in res.text.lower()

    row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == "order_TEST1").first()
    assert row is not None
    assert row.status == "created"
    assert float(row.amount) == 1000.0
    assert row.bill_id == bill.id


def test_create_order_partial_amount_allowed(client, tenant_auth, bill, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id, "amount": 400}, headers=tenant_auth)
    assert res.status_code == 200, res.text
    assert res.json()["amount"] == 40000


def test_create_order_amount_capped_at_pending(client, tenant_auth, bill, db, mock_order_create):
    """Client-requested amount above the pending balance must be rejected,
    not silently capped - silently capping could surprise a tenant into
    paying less than they intended without knowing why."""
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id, "amount": 5000}, headers=tenant_auth)
    assert res.status_code == 400
    assert "pending" in res.json()["detail"].lower()
    assert not mock_order_create   # never even reached Razorpay


def test_create_order_below_minimum_paise_rejected(client, tenant_auth, bill, db, mock_order_create):
    _enable_razorpay(db)
    # Bill is Rs 1000 pending; request 0.50 rupees = 50 paise, below the 100 minimum.
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id, "amount": 0.5}, headers=tenant_auth)
    assert res.status_code == 400
    assert "minimum" in res.json()["detail"].lower()


def test_create_order_rejects_other_tenants_bill(client, other_tenant_auth, bill, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=other_tenant_auth)
    assert res.status_code == 403
    assert not mock_order_create


def test_create_order_on_fully_paid_bill_rejected(client, tenant_auth, bill, db, mock_order_create):
    _enable_razorpay(db)
    bill.pending_amount = 0
    bill.paid_amount = bill.amount
    bill.status = "paid"
    db.commit()

    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=tenant_auth)
    assert res.status_code == 400
    assert "fully paid" in res.json()["detail"].lower()


def test_create_order_missing_bill_404(client, tenant_auth, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": 999999}, headers=tenant_auth)
    assert res.status_code == 404


def test_create_order_requires_auth(client, bill, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order", json={"bill_id": bill.id})
    assert res.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════════════════════
# VERIFY SIGNATURE
# ══════════════════════════════════════════════════════════════════════════════

def _create_order(client, tenant_auth, bill_id, amount=None):
    payload = {"bill_id": bill_id}
    if amount is not None:
        payload["amount"] = amount
    res = client.post("/api/tenant/payments/razorpay/create-order", json=payload, headers=tenant_auth)
    assert res.status_code == 200, res.text
    return res.json()


def test_verify_success_creates_payment_and_reconciles_bill(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)

    order = _create_order(client, tenant_auth, bill.id)

    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_TEST1",
        "razorpay_signature": "whatever-the-sdk-accepts-in-this-mock",
    }, headers=tenant_auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert len(body["bills"]) == 1
    assert body["bills"][0]["status"] == "paid"
    assert body["bills"][0]["pending_amount"] == 0.0
    assert len(body["payments"]) == 1

    db.refresh(bill)
    assert bill.status == "paid"
    assert float(bill.pending_amount) == 0.0

    pay = db.query(Payment).filter(Payment.razorpay_order_id == order["order_id"]).first()
    assert pay is not None
    assert pay.razorpay_payment_id == "pay_TEST1"
    assert pay.payment_method == "Razorpay"
    assert float(pay.amount) == 1000.0

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "paid"
    assert order_row.payment_id == pay.id


def test_verify_bad_signature_creates_no_payment(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    """The headline security rule: signature mismatch -> 400, nothing paid."""
    _enable_razorpay(db)
    _mock_verify_fails(monkeypatch)

    order = _create_order(client, tenant_auth, bill.id)

    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_FAKE",
        "razorpay_signature": "tampered",
    }, headers=tenant_auth)
    assert res.status_code == 400

    db.refresh(bill)
    assert bill.status == "pending"
    assert float(bill.pending_amount) == 1000.0
    assert db.query(Payment).count() == 0

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "failed"


def test_verify_unknown_order_404(client, tenant_auth, db, monkeypatch):
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": "order_DOES_NOT_EXIST",
        "razorpay_payment_id": "pay_x",
        "razorpay_signature": "sig",
    }, headers=tenant_auth)
    assert res.status_code == 404


def test_verify_rejects_other_tenants_order(client, tenant_auth, other_tenant_auth, bill, db, mock_order_create, monkeypatch):
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)   # created by `tenant`

    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_x",
        "razorpay_signature": "sig",
    }, headers=other_tenant_auth)                          # verified by `other_tenant`
    assert res.status_code == 403
    assert db.query(Payment).count() == 0


def test_verify_cannot_be_replayed(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    """Same order verified twice must not create two Payment rows."""
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)

    payload = {
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_TEST1",
        "razorpay_signature": "sig",
    }
    first = client.post("/api/tenant/payments/razorpay/verify", json=payload, headers=tenant_auth)
    assert first.status_code == 200, first.text

    second = client.post("/api/tenant/payments/razorpay/verify", json=payload, headers=tenant_auth)
    assert second.status_code == 409

    assert db.query(Payment).filter(Payment.razorpay_order_id == order["order_id"]).count() == 1


def test_verify_partial_payment_leaves_bill_partial(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id, amount=400)

    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_TEST1",
        "razorpay_signature": "sig",
    }, headers=tenant_auth)
    assert res.status_code == 200, res.text
    assert res.json()["bills"][0]["status"] == "partial"
    assert res.json()["bills"][0]["pending_amount"] == 600.0


# ══════════════════════════════════════════════════════════════════════════════
# PAY TOTAL BALANCE (bill_id omitted - Home screen "Pay bill")
#
# Same two endpoints, just without a bill_id: the amount is capped at the
# tenant's WHOLE pending balance, and verify() FIFO-allocates it across
# every unpaid bill (oldest due date first), automatically - no admin
# review step, since a verified signature is already proof of payment.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def two_bills(db, tenant, shop):
    """Older bill (due first) and a newer one, both unpaid."""
    older = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Rent",
                 amount=1000, paid_amount=0, pending_amount=1000, status="pending",
                 due_date=__import__("datetime").datetime(2026, 6, 1))
    newer = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Electricity",
                 amount=600, paid_amount=0, pending_amount=600, status="pending",
                 due_date=__import__("datetime").datetime(2026, 7, 1))
    db.add(older); db.add(newer)
    db.commit()
    db.refresh(older); db.refresh(newer)
    return older, newer


def test_create_order_total_balance_no_bill_id(client, tenant_auth, two_bills, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={}, headers=tenant_auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["amount"] == 160000     # 1000 + 600 = 1600 rupees -> paise
    assert body["bill_id"] is None

    row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == body["order_id"]).first()
    assert row.bill_id is None
    assert float(row.amount) == 1600.0


def test_create_order_total_balance_no_pending_bills(client, tenant_auth, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order", json={}, headers=tenant_auth)
    assert res.status_code == 400
    assert "no pending bills" in res.json()["detail"].lower()


def test_create_order_total_balance_partial_capped(client, tenant_auth, two_bills, db, mock_order_create):
    _enable_razorpay(db)
    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"amount": 5000}, headers=tenant_auth)   # only 1600 is owed
    assert res.status_code == 400
    assert "pending" in res.json()["detail"].lower()


def test_verify_total_balance_pays_full_amount_fifo(client, tenant_auth, two_bills, db, mock_order_create, monkeypatch):
    """Full balance (1600) should fully clear both bills, oldest first."""
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    older, newer = two_bills

    order = _create_order(client, tenant_auth, bill_id=None)
    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_ALL",
        "razorpay_signature": "sig",
    }, headers=tenant_auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["payments"]) == 2
    assert {b["id"] for b in body["bills"]} == {older.id, newer.id}
    assert all(b["status"] == "paid" for b in body["bills"])

    db.refresh(older); db.refresh(newer)
    assert older.status == "paid" and float(older.pending_amount) == 0.0
    assert newer.status == "paid" and float(newer.pending_amount) == 0.0


def test_verify_total_balance_partial_fills_oldest_bill_first(client, tenant_auth, two_bills, db, mock_order_create, monkeypatch):
    """Rs 1200 of the 1600 owed: the OLDER bill (due first, Rs 1000) should
    be paid off completely, and the remaining Rs 200 should go to the
    newer bill, leaving it partial - not split evenly, not newest-first."""
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    older, newer = two_bills

    order = _create_order(client, tenant_auth, bill_id=None, amount=1200)
    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_PARTIAL_ALL",
        "razorpay_signature": "sig",
    }, headers=tenant_auth)
    assert res.status_code == 200, res.text

    db.refresh(older); db.refresh(newer)
    assert older.status == "paid" and float(older.pending_amount) == 0.0
    assert newer.status == "partial" and float(newer.pending_amount) == 400.0

    pays = db.query(Payment).filter(Payment.razorpay_order_id == order["order_id"]).all()
    assert len(pays) == 2
    by_bill = {p.bill_id: float(p.amount) for p in pays}
    assert by_bill[older.id] == 1000.0
    assert by_bill[newer.id] == 200.0


def test_verify_total_balance_race_overpays_last_bill_instead_of_losing_money(
    client, tenant_auth, two_bills, db, mock_order_create, monkeypatch,
):
    """If pending bills get paid down by something else between create-order
    and verify, the verified amount must still be recorded somewhere, not
    dropped - even if that means briefly overpaying the last bill touched."""
    _enable_razorpay(db)
    _mock_verify_ok(monkeypatch)
    older, newer = two_bills

    order = _create_order(client, tenant_auth, bill_id=None)   # locks in 1600

    # Simulate an admin manually recording a cash payment on `newer` in the
    # meantime, so only 1000 is actually outstanding by the time verify runs.
    newer.pending_amount = 0
    newer.paid_amount = newer.amount
    newer.status = "paid"
    db.commit()

    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_RACE",
        "razorpay_signature": "sig",
    }, headers=tenant_auth)
    assert res.status_code == 200, res.text

    total_recorded = sum(
        float(p.amount) for p in
        db.query(Payment).filter(Payment.razorpay_order_id == order["order_id"]).all()
    )
    assert total_recorded == 1600.0   # the full verified amount is accounted for, nowhere lost


def test_verify_total_balance_no_bills_at_all_is_a_500_not_silent_loss(client, tenant_auth, db, mock_order_create, monkeypatch):
    """Edge case: every bill vanishes between create-order and verify (should
    be near-impossible in practice) - must fail loudly with the payment ID
    for manual reconciliation, never silently succeed with nothing recorded."""
    _enable_razorpay(db)
    from create_tables import User, Shop, UserShop
    # Build a fresh tenant+shop+bill dedicated to this test to avoid cross-test coupling.
    t = User(name="Solo Tenant", mobile="9000099999", email="solo@test.com",
             password_hash="x", role="tenant", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    s = Shop(shop_number="Z-1", status="occupied", shop_rent=100, shop_deposit=100)
    db.add(s); db.commit(); db.refresh(s)
    db.add(UserShop(user_id=t.id, shop_id=s.id)); db.commit()
    b = Bill(user_id=t.id, shop_id=s.id, bill_type="Rent", amount=500,
             paid_amount=0, pending_amount=500, status="pending")
    db.add(b); db.commit(); db.refresh(b)

    import auth_service
    solo_auth = {"Authorization": f"Bearer {auth_service.create_access_token({'sub': str(t.id)})}"}
    monkeypatch.setattr(razorpay.utility.utility.Utility, "verify_payment_signature", lambda self, p: True)

    order = _create_order(client, solo_auth, bill_id=None)

    # The bill disappears (e.g. deleted) before verify runs.
    db.delete(b)
    db.commit()

    res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_ORPHAN",
        "razorpay_signature": "sig",
    }, headers=solo_auth)
    assert res.status_code == 500
    assert "pay_ORPHAN" in res.json()["detail"]
    assert db.query(Payment).filter(Payment.razorpay_payment_id == "pay_ORPHAN").count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# DB-CONFIGURED KEYS (Settings, not .env)
#
# payment.razorpay_key_id / payment.razorpay_key_secret let an admin paste
# the Razorpay keys into Settings instead of editing a server-side .env -
# important for deployments where files get wholesale-replaced on every
# release (Jenkins etc.) and hand-editing .env on the box isn't practical.
# ══════════════════════════════════════════════════════════════════════════════

import razorpay_service


def _set_db_keys(db, key_id="rzp_test_DBKEY", key_secret="db_secret_value"):
    settings_service.set_many(db, {
        "payment.razorpay_key_id": key_id,
        "payment.razorpay_key_secret": key_secret,
    })
    db.commit()
    settings_service.invalidate_cache()


def test_payment_works_from_db_keys_alone_no_env_needed(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    """Blank out the env fallback entirely - DB-configured keys must still work."""
    monkeypatch.setattr(razorpay_service, "RAZORPAY_KEY_ID_ENV", "")
    monkeypatch.setattr(razorpay_service, "RAZORPAY_KEY_SECRET_ENV", "")
    _enable_razorpay(db)
    _set_db_keys(db)

    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=tenant_auth)
    assert res.status_code == 200, res.text
    assert res.json()["key_id"] == "rzp_test_DBKEY"


def test_no_env_and_no_db_keys_stays_off(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    monkeypatch.setattr(razorpay_service, "RAZORPAY_KEY_ID_ENV", "")
    monkeypatch.setattr(razorpay_service, "RAZORPAY_KEY_SECRET_ENV", "")
    _enable_razorpay(db)   # switch is on, but genuinely no keys anywhere

    res = client.get("/api/settings/public")
    assert res.json()["razorpay_enabled"] is False

    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=tenant_auth)
    assert res.status_code == 503


def test_db_key_id_takes_priority_over_env(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    monkeypatch.setattr(razorpay_service, "RAZORPAY_KEY_ID_ENV", "rzp_test_FROM_ENV")
    monkeypatch.setattr(razorpay_service, "RAZORPAY_KEY_SECRET_ENV", "env_secret")
    _enable_razorpay(db)
    _set_db_keys(db, key_id="rzp_test_FROM_DB")

    res = client.post("/api/tenant/payments/razorpay/create-order",
                       json={"bill_id": bill.id}, headers=tenant_auth)
    assert res.status_code == 200, res.text
    assert res.json()["key_id"] == "rzp_test_FROM_DB"


# ══════════════════════════════════════════════════════════════════════════════
# SECRET MASKING (GET/PUT /api/settings)
# ══════════════════════════════════════════════════════════════════════════════

def test_get_settings_never_echoes_secret_value(client, admin_auth, db):
    _set_db_keys(db, key_secret="super-secret-value")
    res = client.get("/api/settings", headers=admin_auth)
    assert res.status_code == 200
    assert "super-secret-value" not in res.text

    item = next(s for s in res.json()["settings"] if s["key"] == "payment.razorpay_key_secret")
    assert item["value"] == ""
    assert item["is_set"] is True


def test_get_settings_is_set_false_when_never_configured(client, admin_auth, db):
    res = client.get("/api/settings", headers=admin_auth)
    item = next(s for s in res.json()["settings"] if s["key"] == "payment.razorpay_key_secret")
    assert item["is_set"] is False


def test_put_settings_blank_secret_does_not_clear_existing(client, admin_auth, db):
    _set_db_keys(db, key_secret="keep-me")

    res = client.put("/api/settings", headers=admin_auth, json={
        "values": {"payment.razorpay_key_secret": "", "app.name": "New Name"},
    })
    assert res.status_code == 200, res.text
    assert "payment.razorpay_key_secret" not in res.json()["changed"]

    settings_service.invalidate_cache()
    assert settings_service.get_all(db)["payment.razorpay_key_secret"] == "keep-me"
    assert settings_service.get_all(db)["app.name"] == "New Name"


def test_put_settings_new_secret_value_replaces_old(client, admin_auth, db):
    _set_db_keys(db, key_secret="old-value")

    res = client.put("/api/settings", headers=admin_auth, json={
        "values": {"payment.razorpay_key_secret": "new-value"},
    })
    assert res.status_code == 200, res.text
    assert "payment.razorpay_key_secret" in res.json()["changed"]

    settings_service.invalidate_cache()
    assert settings_service.get_all(db)["payment.razorpay_key_secret"] == "new-value"


def test_tenant_cannot_read_or_write_settings_with_secrets(client, tenant_auth):
    assert client.get("/api/settings", headers=tenant_auth).status_code == 403
    assert client.put("/api/settings", headers=tenant_auth,
                       json={"values": {"payment.razorpay_key_secret": "x"}}).status_code == 403
