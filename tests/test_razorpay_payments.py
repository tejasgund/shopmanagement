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
    assert body["bill"]["status"] == "paid"
    assert body["bill"]["pending_amount"] == 0.0

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
    assert res.json()["bill"]["status"] == "partial"
    assert res.json()["bill"]["pending_amount"] == 600.0
