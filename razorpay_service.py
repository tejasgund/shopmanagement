"""
razorpay_service.py - Razorpay Standard Checkout business logic: credential
resolution, the SDK client, and the shared "this order has been proven paid,
now actually apply the money" core used by both verify() and the webhook.

Extracted from app.py (step 22 of the router/service split), together with
routers/razorpay.py. Unlike most of this split, this one departs from the
established pattern of leaving Razorpay logic in app.py: previously it stayed
there because RAZORPAY_KEY_ID_ENV / RAZORPAY_KEY_SECRET_ENV /
RAZORPAY_WEBHOOK_SECRET_ENV are module-level constants that several tests
monkeypatch directly (monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID_ENV",
...)). Moving them here means those tests now patch
razorpay_service.RAZORPAY_KEY_ID_ENV/etc instead - same assertions, same
simulated scenarios, just patching the module that actually reads them now.
See tests/test_razorpay_payments.py and tests/test_razorpay_webhook.py.

Nothing about behavior changed in this move: same Settings-first/env-fallback
credential resolution, same FIFO allocation algorithm, same "whichever of
verify()/webhook reaches an order first wins" race handling.
"""

import os
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

import razorpay

from create_tables import Bill, Payment, RazorpayOrder
from domain_helpers import _decimal_to_float, _reconcile_bill
from audit_service import write_audit

# ──────────────────────────────────────────────
# Razorpay settings
# The admin-editable values in Settings (payment.razorpay_key_id/_secret) are
# the primary source - see settings_service.py's docstring for why this pair
# is a deliberate exception to "secrets live in env only". These two env vars
# are kept ONLY as a fallback for deployments that still prefer .env (or
# haven't set the DB values yet); see _razorpay_credentials() below, which is
# what every request actually calls. No default: an unset secret must fail
# closed, never silently sign with "".
# ──────────────────────────────────────────────
RAZORPAY_KEY_ID_ENV     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET_ENV = os.getenv("RAZORPAY_KEY_SECRET", "")
# Separate secret used only to verify /api/webhooks/razorpay calls really came
# from Razorpay (HMAC over the raw request body) - not the same value as
# RAZORPAY_KEY_SECRET above, which signs API calls we make outbound.
RAZORPAY_WEBHOOK_SECRET_ENV = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")


def _razorpay_credentials(cfg: dict) -> tuple:
    """
    (key_id, key_secret), preferring the admin-editable Settings values
    (payment.razorpay_key_id/_secret) and falling back to the RAZORPAY_KEY_ID/
    RAZORPAY_KEY_SECRET env vars only where the DB value is blank - lets a
    deployment migrate from .env to Settings whenever it's convenient, not
    all at once.
    """
    key_id = str(cfg.get("payment.razorpay_key_id") or "").strip() or RAZORPAY_KEY_ID_ENV
    key_secret = str(cfg.get("payment.razorpay_key_secret") or "").strip() or RAZORPAY_KEY_SECRET_ENV
    return key_id, key_secret


def _razorpay_webhook_secret(cfg: dict) -> str:
    """
    The secret configured on the Razorpay dashboard's Webhooks page for
    /api/webhooks/razorpay - NOT the same value as the Key Secret (that one
    signs requests we make outbound; this one lets us verify requests
    Razorpay makes inbound). Same Settings-first, env-fallback pattern as
    _razorpay_credentials.
    """
    return str(cfg.get("payment.razorpay_webhook_secret") or "").strip() or RAZORPAY_WEBHOOK_SECRET_ENV


def _razorpay_public_config(cfg: dict) -> tuple:
    """
    (enabled, key_id) as the frontend should see them. Single source of
    truth for both /api/settings/public and the /api/tenant/home bundle, so
    the two can't quietly disagree about whether "Pay online" should show.
    Needs the admin's switch on AND real keys configured (Settings or env) -
    a half-configured server looks "off" to tenants, never errors on them.
    """
    key_id, key_secret = _razorpay_credentials(cfg)
    ready = bool(cfg.get("payment.razorpay_enabled")) and bool(key_id) and bool(key_secret)
    return ready, (key_id if ready else None)


def credentials_fingerprint(cfg: dict) -> str:
    """
    A safe one-line description of WHICH credentials a request actually used,
    for the log when the gateway refuses them.

    The key id is already public - it is handed to checkout.js in the browser -
    so it is written out in full. The secret never is: only whether one is set
    and how long it is. That is enough to spot the usual causes (a blank or
    truncated secret, a key and secret from different pairs) without putting
    the secret itself into a log file someone may later paste elsewhere.

    It also reports WHERE each half came from. _razorpay_credentials() prefers
    the Settings value over the environment one, so a deployment that updated
    .env while a stale value still sits in Settings keeps using the stale one -
    silently, and that is not visible from any other log line.
    """
    settings_id = str(cfg.get("payment.razorpay_key_id") or "").strip()
    settings_secret = str(cfg.get("payment.razorpay_key_secret") or "").strip()
    key_id, key_secret = _razorpay_credentials(cfg)

    id_source = "Settings" if settings_id else ("env" if RAZORPAY_KEY_ID_ENV else "nowhere")
    secret_source = "Settings" if settings_secret else ("env" if RAZORPAY_KEY_SECRET_ENV else "nowhere")
    secret_desc = f"set ({len(key_secret)} chars)" if key_secret else "NOT SET"

    return (
        f"key_id={key_id or '(none)'} [from {id_source}], "
        f"key_secret={secret_desc} [from {secret_source}]"
    )


def _razorpay_client(cfg: dict) -> "razorpay.Client":
    key_id, key_secret = _razorpay_credentials(cfg)
    if not key_id or not key_secret:
        # Distinct from "feature turned off" - this is a deployment gap the
        # admin needs to fix (Settings -> Online payments, or the server's
        # .env), not a business decision.
        raise HTTPException(503, detail="Online payments are not configured on this server.")
    return razorpay.Client(auth=(key_id, key_secret))


def _tenant_total_pending(db: Session, user_id: int) -> Decimal:
    """Sum of pending_amount across every unpaid/partial bill for this tenant."""
    total = sum(
        _decimal_to_float(b.pending_amount)
        for b in db.query(Bill).filter(Bill.user_id == user_id, Bill.status.in_(["pending", "partial"])).all()
    )
    return Decimal(str(total)).quantize(Decimal("0.01"))


def _allocate_razorpay_payment(
    db: Session, bills: list, amount: Decimal, razorpay_order_id: str, razorpay_payment_id: str, actor_id: int,
) -> list:
    """
    FIFO-allocate `amount` across `bills` (already row-locked, given in the
    order they should be paid first - oldest due date first for a
    whole-balance payment, or just the one bill for a single-bill payment),
    creating one Payment per bill that receives money and reconciling each
    via the same _reconcile_bill() every other payment path uses. Mirrors
    auto_allocate_confirm's algorithm exactly, but for one Razorpay-verified
    amount instead of admin-entered cash - fully automatic, no admin review
    step, since the signature already proved the money was actually paid.

    If the tenant's total pending balance shrank between create-order and
    verify (e.g. an admin recorded a manual payment in between), any
    leftover is applied on top of the last bill touched rather than being
    silently dropped - real money that was actually charged is never lost,
    even if that means a bill ends up briefly overpaid (the existing manual
    payment path already tolerates overpayment the same way).
    """
    remaining = amount
    payments = []
    now = datetime.now(timezone.utc)

    for bill in bills:
        if remaining <= 0:
            break
        outstanding = Decimal(str(_decimal_to_float(bill.pending_amount))).quantize(Decimal("0.01"))
        if outstanding <= 0:
            continue
        alloc = min(remaining, outstanding)

        pay = Payment(
            bill_id=bill.id, amount=alloc, payment_method="Razorpay",
            remarks=f"Razorpay payment {razorpay_payment_id}",
            payment_date=now,
            razorpay_order_id=razorpay_order_id, razorpay_payment_id=razorpay_payment_id,
        )
        db.add(pay)
        db.flush()
        db.refresh(bill)
        _reconcile_bill(bill)
        payments.append(pay)

        write_audit(db, actor_id, "CREATE", "payments", pay.id, new_data={
            "bill_id": bill.id, "amount": float(alloc), "payment_method": "Razorpay",
            "razorpay_order_id": razorpay_order_id, "razorpay_payment_id": razorpay_payment_id,
        })
        remaining -= alloc

    if remaining > 0 and payments:
        last = payments[-1]
        last.amount = Decimal(str(_decimal_to_float(last.amount))) + remaining
        db.flush()
        last_bill = db.query(Bill).filter(Bill.id == last.bill_id).first()
        db.refresh(last_bill)
        _reconcile_bill(last_bill)

    return payments


class _RazorpayNoPendingBillsError(Exception):
    """Raised when a Razorpay order was proven paid but there is nothing left to apply it to."""


def _finalize_paid_razorpay_order(
    db: Session, order_row: "RazorpayOrder", razorpay_payment_id: str, actor_id: int,
) -> list:
    """
    Shared core of "this order has now been proven paid - actually allocate
    the money to bills and flip the order to 'paid'". Two independent paths
    can reach here for the same order: the tenant's own browser calling
    verify() right after checkout (HMAC-verified via the Key Secret), or
    Razorpay's server-to-server webhook (HMAC-verified via the Webhook
    Secret) reporting the same payment.captured event. Whichever gets here
    first while order_row.status is still "created" wins; callers must check
    that status themselves before calling this, and the caller that loses
    the race simply never gets a "created" order to act on - so the same
    payment can never be recorded twice no matter which path arrives first
    or how many times either one retries.

    Raises _RazorpayNoPendingBillsError if there's nowhere to apply the money
    (e.g. every bill was settled some other way in between) - callers decide
    how to surface that, since a webhook has no user to show an error to but
    verify() does.
    """
    if order_row.bill_id is not None:
        bills = db.query(Bill).filter(Bill.id == order_row.bill_id).with_for_update().all()
    else:
        bills = (
            db.query(Bill)
            .filter(Bill.user_id == order_row.user_id, Bill.status.in_(["pending", "partial"]))
            .order_by(Bill.due_date.is_(None), Bill.due_date.asc(), Bill.bill_date.asc(), Bill.id.asc())
            .with_for_update()
            .all()
        )

    if not bills:
        raise _RazorpayNoPendingBillsError("no pending bills to apply this payment to")

    payments = _allocate_razorpay_payment(
        db, bills, order_row.amount, order_row.razorpay_order_id, razorpay_payment_id, actor_id,
    )
    if not payments:
        raise RuntimeError("allocation produced no payments")

    order_row.status = "paid"
    order_row.payment_id = payments[0].id
    db.commit()
    for p in payments:
        db.refresh(p)
    return payments
