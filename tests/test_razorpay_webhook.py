"""
Tests for POST /api/webhooks/razorpay - the server-to-server counterpart to
/api/tenant/payments/razorpay/verify (see that route's docstring in app.py).

The rules these lock down, in priority order:
  1. A request whose signature doesn't check out must never touch the
     database, no matter what event it claims to carry.
  2. payment.captured records the payment (same allocation path as verify())
     ONLY if the order is still "created" - whichever of verify()/webhook
     gets there first wins, the other is always a safe no-op. This is the
     whole point of the endpoint: catching a payment verify() never saw
     (closed browser, dropped network) without ever double-crediting one
     verify() already recorded.
  3. payment.failed marks a still-open order "failed" so it doesn't linger
     as "created" forever.
  4. Nothing here ever 500s for conditions Razorpay itself might legitimately
     retry on (unknown order, already-handled order, missing config aside) -
     a webhook has no user to show an error to, so failures are logged and
     acknowledged with 200 instead of triggering endless Razorpay retries.
"""
import razorpay
import razorpay.errors
import pytest

from models.schema import Bill, Payment, RazorpayOrder
from services import settings as settings_service
from test_razorpay_payments import _enable_razorpay, bill, mock_order_create, _create_order


WEBHOOK_URL = "/api/webhooks/razorpay"


def _enable_razorpay_webhook(db, secret="whsec_test123"):
    _enable_razorpay(db)
    settings_service.set_many(db, {"payment.razorpay_webhook_secret": secret})
    db.commit()
    settings_service.invalidate_cache()


def _mock_webhook_verify_ok(monkeypatch):
    monkeypatch.setattr(
        razorpay.utility.utility.Utility, "verify_webhook_signature",
        lambda self, body, signature, secret: True,
    )


def _mock_webhook_verify_fails(monkeypatch):
    def fail(self, body, signature, secret):
        raise razorpay.errors.SignatureVerificationError("Razorpay Signature Verification Failed")
    monkeypatch.setattr(razorpay.utility.utility.Utility, "verify_webhook_signature", fail)


def _payload(event, order_id, payment_id="pay_WEBHOOK1"):
    return {
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "status": "captured" if event == "payment.captured" else "failed",
                }
            }
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# SIGNATURE / CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def test_webhook_bad_signature_rejected(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    _enable_razorpay_webhook(db)
    _mock_webhook_verify_fails(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)

    res = client.post(WEBHOOK_URL, json=_payload("payment.captured", order["order_id"]),
                       headers={"X-Razorpay-Signature": "tampered"})
    assert res.status_code == 400

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "created"     # untouched
    assert db.query(Payment).count() == 0


def test_webhook_missing_secret_returns_503(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    """Enabled online payments but never set a webhook secret -> fail closed,
    same spirit as _razorpay_client's 'not configured' 503 for the key pair."""
    from services import razorpay as razorpay_service
    _enable_razorpay(db)   # note: no webhook secret set
    monkeypatch.setattr(razorpay_service, "RAZORPAY_WEBHOOK_SECRET_ENV", "")
    order = _create_order(client, tenant_auth, bill.id)

    res = client.post(WEBHOOK_URL, json=_payload("payment.captured", order["order_id"]),
                       headers={"X-Razorpay-Signature": "sig"})
    assert res.status_code == 503


# ══════════════════════════════════════════════════════════════════════════════
# payment.captured
# ══════════════════════════════════════════════════════════════════════════════

def test_webhook_captured_records_payment_when_verify_never_ran(
    client, tenant_auth, bill, db, mock_order_create, monkeypatch,
):
    """The main scenario this endpoint exists for: the tenant's browser never
    called verify() (closed tab / dropped network), but Razorpay confirms the
    money was captured - the webhook must record it on its own."""
    _enable_razorpay_webhook(db)
    _mock_webhook_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)

    res = client.post(WEBHOOK_URL, json=_payload("payment.captured", order["order_id"], "pay_NOVERIFY"),
                       headers={"X-Razorpay-Signature": "sig"})
    assert res.status_code == 200, res.text

    db.refresh(bill)
    assert bill.status == "paid"
    assert float(bill.pending_amount) == 0.0

    pay = db.query(Payment).filter(Payment.razorpay_payment_id == "pay_NOVERIFY").first()
    assert pay is not None
    assert pay.payment_method == "Razorpay"
    assert float(pay.amount) == 1000.0

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "paid"
    assert order_row.payment_id == pay.id


def test_webhook_captured_is_idempotent_on_replay(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    """Razorpay retries webhook delivery until it gets a 2xx - a second
    delivery of the same event must never create a second Payment."""
    _enable_razorpay_webhook(db)
    _mock_webhook_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)
    payload = _payload("payment.captured", order["order_id"], "pay_REPLAY")

    res1 = client.post(WEBHOOK_URL, json=payload, headers={"X-Razorpay-Signature": "sig"})
    res2 = client.post(WEBHOOK_URL, json=payload, headers={"X-Razorpay-Signature": "sig"})
    assert res1.status_code == 200
    assert res2.status_code == 200

    assert db.query(Payment).filter(Payment.razorpay_payment_id == "pay_REPLAY").count() == 1
    db.refresh(bill)
    assert float(bill.pending_amount) == 0.0     # not double-paid into negative


def test_webhook_captured_is_a_no_op_if_verify_already_recorded_it(
    client, tenant_auth, bill, db, mock_order_create, monkeypatch,
):
    """The normal case: the tenant's browser called verify() first (order is
    already 'paid'); the webhook arriving afterwards for the same order must
    not create a second Payment."""
    _enable_razorpay_webhook(db)
    monkeypatch.setattr(
        razorpay.utility.utility.Utility, "verify_payment_signature", lambda self, p: True,
    )
    order = _create_order(client, tenant_auth, bill.id)

    verify_res = client.post("/api/tenant/payments/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_ALREADYVERIFIED",
        "razorpay_signature": "sig",
    }, headers=tenant_auth)
    assert verify_res.status_code == 200, verify_res.text

    _mock_webhook_verify_ok(monkeypatch)
    res = client.post(
        WEBHOOK_URL,
        json=_payload("payment.captured", order["order_id"], "pay_ALREADYVERIFIED"),
        headers={"X-Razorpay-Signature": "sig"},
    )
    assert res.status_code == 200

    assert db.query(Payment).filter(Payment.razorpay_payment_id == "pay_ALREADYVERIFIED").count() == 1
    db.refresh(bill)
    assert float(bill.pending_amount) == 0.0


def test_webhook_captured_unknown_order_returns_200_without_erroring(client, db, monkeypatch):
    """An order_id this app never created (different integration on the same
    Razorpay account, stale test event, etc.) is logged and acknowledged, not
    a crash - there's nothing actionable to do with it here."""
    _enable_razorpay_webhook(db)
    _mock_webhook_verify_ok(monkeypatch)

    res = client.post(WEBHOOK_URL, json=_payload("payment.captured", "order_DOES_NOT_EXIST"),
                       headers={"X-Razorpay-Signature": "sig"})
    assert res.status_code == 200
    assert db.query(Payment).count() == 0


def test_webhook_captured_no_pending_bills_leaves_order_created_for_manual_reconciliation(
    client, tenant_auth, db, mock_order_create, monkeypatch,
):
    """Mirrors test_verify_total_balance_no_bills_at_all_is_a_500_not_silent_loss,
    but from the webhook side: money was captured with nowhere to apply it
    (e.g. the bill was deleted in between) - must not silently vanish, and
    must not crash the webhook either. The order stays 'created' so an admin
    can reconcile it manually with the payment ID from the logs."""
    _enable_razorpay_webhook(db)
    from models.schema import User, Shop, UserShop
    t = User(name="Webhook Solo Tenant", mobile="9000088888", email="wsolo@test.com",
             password_hash="x", role="tenant", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    s = Shop(shop_number="W-1", status="occupied", shop_rent=100, shop_deposit=100)
    db.add(s); db.commit(); db.refresh(s)
    db.add(UserShop(user_id=t.id, shop_id=s.id)); db.commit()
    b = Bill(user_id=t.id, shop_id=s.id, bill_type="Rent", amount=500,
             paid_amount=0, pending_amount=500, status="pending")
    db.add(b); db.commit(); db.refresh(b)

    from core import security as auth_service
    solo_auth = {"Authorization": f"Bearer {auth_service.create_access_token({'sub': str(t.id)})}"}
    order = _create_order(client, solo_auth, bill_id=None)

    db.delete(b)
    db.commit()

    _mock_webhook_verify_ok(monkeypatch)
    res = client.post(WEBHOOK_URL, json=_payload("payment.captured", order["order_id"], "pay_ORPHAN_WH"),
                       headers={"X-Razorpay-Signature": "sig"})
    assert res.status_code == 200     # acknowledged, not a 500 - no user to show an error to

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "created"      # left open for manual reconciliation
    assert db.query(Payment).filter(Payment.razorpay_payment_id == "pay_ORPHAN_WH").count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# payment.failed / other events
# ══════════════════════════════════════════════════════════════════════════════

def test_webhook_failed_marks_order_failed(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    _enable_razorpay_webhook(db)
    _mock_webhook_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)

    res = client.post(WEBHOOK_URL, json=_payload("payment.failed", order["order_id"], "pay_FAILEDWH"),
                       headers={"X-Razorpay-Signature": "sig"})
    assert res.status_code == 200

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "failed"
    assert db.query(Payment).count() == 0


def test_webhook_ignores_unhandled_event_types(client, tenant_auth, bill, db, mock_order_create, monkeypatch):
    """Events we haven't subscribed handling for (e.g. order.paid) are
    acknowledged, not errored - the dashboard may have more boxes checked
    than this app currently acts on."""
    _enable_razorpay_webhook(db)
    _mock_webhook_verify_ok(monkeypatch)
    order = _create_order(client, tenant_auth, bill.id)

    res = client.post(WEBHOOK_URL, json=_payload("order.paid", order["order_id"], "pay_IGNORED"),
                       headers={"X-Razorpay-Signature": "sig"})
    assert res.status_code == 200

    order_row = db.query(RazorpayOrder).filter(RazorpayOrder.razorpay_order_id == order["order_id"]).first()
    assert order_row.status == "created"     # unaffected
