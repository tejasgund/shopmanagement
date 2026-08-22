"""
routers/razorpay.py - Razorpay Standard Checkout: tenant pays online, either
one bill (from the bill detail sheet) or the WHOLE pending balance in one go
(from Home) - both go through the same two steps:

  1. Tenant taps "Pay online" / "Pay bill" -> create-order (this decides
     and locks in the amount server-side, and which bill(s) it can apply
     to - a single bill, or every bill this tenant owes on).
  2. Browser opens the Razorpay modal with that order_id.
  3. On success, Razorpay hands the browser razorpay_payment_id/order_id/
     signature -> verify (this is the ONLY place any Payment row gets
     created from this flow; nothing is ever marked paid off a
     signature-less client claim). One payment can become several
     Payment rows if it's spread across bills - see
     _allocate_razorpay_payment in razorpay_service.py.

Also hosts the /api/webhooks/razorpay server-to-server callback, which
independently catches payments the tenant's own browser never got to report
(closed tab, dropped network, crashed app) via the same shared finalize path.

Extracted verbatim from app.py (step 22 of the router/service split, along
with razorpay_service.py - see that module's docstring for why the Razorpay
logic breaks from the "leave it in app.py" precedent other modules follow).
"""

import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import razorpay
import razorpay.errors

from datetime import datetime, timezone

from db_config import get_db
from create_tables import Bill, RazorpayOrder, User
from auth_service import require_tenant
from audit_service import write_audit
from domain_helpers import _decimal_to_float
from schemas import (
    PaymentResponse, RazorpayCreateOrderRequest, RazorpayCreateOrderResponse, RazorpayVerifyRequest,
)
import settings_service
from log import get_logger
from razorpay_service import (
    _RazorpayNoPendingBillsError, _finalize_paid_razorpay_order, _razorpay_client,
    _razorpay_credentials, _razorpay_webhook_secret, _tenant_total_pending,
)

logger = get_logger("app")

router = APIRouter(tags=["Tenant"])


@router.post("/api/tenant/payments/razorpay/create-order",
             response_model=RazorpayCreateOrderResponse, tags=["Tenant"])
def create_razorpay_order(
    body:         RazorpayCreateOrderRequest,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """
    Step 1 of Razorpay Standard Checkout. The amount actually charged is
    decided HERE - never trusted from the client again - and stored in
    razorpay_orders so verify() has something authoritative to check
    against later.
    """
    cfg = settings_service.get_all(db)
    if not cfg.get("payment.razorpay_enabled"):
        raise HTTPException(403, detail="Online payments are currently turned off.")

    bill = None
    if body.bill_id is not None:
        bill = db.query(Bill).filter(Bill.id == body.bill_id).first()
        if not bill:
            raise HTTPException(404, detail="Bill not found")
        if bill.user_id != current_user.id:
            raise HTTPException(403, detail="This bill does not belong to you.")
        pending = Decimal(str(_decimal_to_float(bill.pending_amount))).quantize(Decimal("0.01"))
        if pending <= 0:
            raise HTTPException(400, detail="This bill is already fully paid.")
    else:
        pending = _tenant_total_pending(db, current_user.id)
        if pending <= 0:
            raise HTTPException(400, detail="You have no pending bills to pay.")

    if body.amount is None:
        charge = pending
    else:
        charge = Decimal(str(body.amount)).quantize(Decimal("0.01"))
        if charge <= 0:
            raise HTTPException(400, detail="Amount must be greater than zero.")
        if charge > pending:
            raise HTTPException(
                400, detail=f"Amount cannot exceed the pending balance of {float(pending)}.",
            )

    amount_paise = int(charge * 100)
    if amount_paise < 100:
        raise HTTPException(400, detail="Minimum payable amount is Rs 1 (100 paise).")

    client = _razorpay_client(cfg)
    try:
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"{'bill-' + str(bill.id) if bill else 'balance-' + str(current_user.id)}-"
                       f"{int(datetime.now(timezone.utc).timestamp())}",
            "notes": {
                "bill_id": str(bill.id) if bill else "ALL",
                "user_id": str(current_user.id),
            },
        })
    except razorpay.errors.BadRequestError as exc:
        msg = str(exc)
        if "auth" in msg.lower() or "key" in msg.lower():
            raise HTTPException(401, detail="Payment gateway authentication failed. Contact the office.")
        raise HTTPException(400, detail=f"Payment gateway rejected the request: {msg}")
    except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as exc:
        logger.error("Razorpay order creation failed for user %s: %s", current_user.id, exc)
        raise HTTPException(500, detail="Could not reach the payment gateway. Please try again.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Unexpected error creating Razorpay order for user %s: %s", current_user.id, exc)
        raise HTTPException(500, detail="Could not start the payment. Please try again.")

    order_row = RazorpayOrder(
        razorpay_order_id=order["id"], bill_id=(bill.id if bill else None), user_id=current_user.id,
        amount=charge, currency="INR", status="created",
    )
    db.add(order_row)
    db.flush()
    write_audit(db, current_user.id, "CREATE", "razorpay_orders", order_row.id, new_data={
        "bill_id": bill.id if bill else None, "amount": float(charge), "razorpay_order_id": order["id"],
    })
    db.commit()

    key_id, _ = _razorpay_credentials(cfg)
    return RazorpayCreateOrderResponse(
        order_id=order["id"], amount=amount_paise, currency="INR",
        key_id=key_id, bill_id=(bill.id if bill else None),
    )


@router.post("/api/tenant/payments/razorpay/verify", tags=["Tenant"])
def verify_razorpay_payment(
    body:         RazorpayVerifyRequest,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """
    Step 3 of Razorpay Standard Checkout. Verifies the HMAC-SHA256 signature
    (order_id + "|" + payment_id, signed with KEY_SECRET) via the SDK's own
    utility - Payment rows are only ever created after this succeeds. Bills
    are row-locked for the write so a duplicated/retried verify call can't
    double-record the same order.

    This only runs on the tenant's own device, so it's skipped entirely if
    their browser closes or loses network right after paying - see the
    /api/webhooks/razorpay route below, which independently catches that
    case from Razorpay's side.
    """
    order_row = (
        db.query(RazorpayOrder)
        .filter(RazorpayOrder.razorpay_order_id == body.razorpay_order_id)
        .first()
    )
    if not order_row:
        raise HTTPException(404, detail="Order not found.")
    if order_row.user_id != current_user.id:
        raise HTTPException(403, detail="This order does not belong to you.")
    if order_row.status != "created":
        # Already verified (or already failed) - never process the same order twice.
        # This is also what happens if the webhook already recorded this
        # exact payment before the tenant's browser got back to us.
        raise HTTPException(409, detail="This payment has already been processed.")

    cfg = settings_service.get_all(db)
    client = _razorpay_client(cfg)
    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id":   body.razorpay_order_id,
            "razorpay_payment_id": body.razorpay_payment_id,
            "razorpay_signature":  body.razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        order_row.status = "failed"
        db.commit()
        raise HTTPException(
            400, detail="Payment could not be verified. If money was deducted, contact the office.",
        )

    try:
        payments = _finalize_paid_razorpay_order(db, order_row, body.razorpay_payment_id, current_user.id)
    except _RazorpayNoPendingBillsError:
        # Verified money with nowhere to apply it (e.g. every bill was
        # settled some other way between create-order and verify) - never
        # silently drop it. Leave the order "created" so it isn't wasted;
        # the office reconciles it manually with the payment ID.
        db.rollback()
        raise HTTPException(
            500,
            detail="Payment was verified but there were no pending bills to apply it to. Contact "
                   f"the office with your payment ID ({body.razorpay_payment_id}).",
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            "Verified Razorpay payment %s (order %s) could not be recorded: %s",
            body.razorpay_payment_id, body.razorpay_order_id, exc,
        )
        raise HTTPException(
            500,
            detail="Payment was verified but could not be recorded. Contact the office with your "
                   f"payment ID ({body.razorpay_payment_id}) so it can be applied manually.",
        )

    touched_bill_ids = [p.bill_id for p in payments]
    touched_bills = db.query(Bill).filter(Bill.id.in_(touched_bill_ids)).all()
    bills_by_id = {b.id: b for b in touched_bills}

    return {
        "success": True,
        "message": "Payment verified and recorded.",
        "payments": [PaymentResponse.model_validate(p) for p in payments],
        "bills": [
            {
                "id": b.id, "status": b.status,
                "paid_amount": _decimal_to_float(b.paid_amount),
                "pending_amount": _decimal_to_float(b.pending_amount),
            }
            for b in (bills_by_id[bid] for bid in dict.fromkeys(touched_bill_ids))
        ],
    }


@router.post("/api/webhooks/razorpay", tags=["Tenant"])
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Server-to-server webhook (configure this URL on the Razorpay dashboard
    under Settings -> Webhooks, with events payment.captured and
    payment.failed checked). This exists alongside verify_razorpay_payment()
    above, not instead of it - the two cover different failure modes:

      - verify() runs on the tenant's own device right after checkout, so it
        can show them a result immediately, but only fires if their browser
        is still open and online when Razorpay's checkout callback returns.
      - This webhook comes from Razorpay's own servers, independent of the
        tenant's device, so it also catches payments that were captured
        successfully but where the browser was closed, the app crashed, or
        the network dropped before verify() could run - money Razorpay
        actually holds that would otherwise sit unrecorded until an admin
        manually reconciled it with the payment ID.

    Whichever of the two reaches a given order first "wins" (flips it from
    "created" to "paid" via the shared _finalize_paid_razorpay_order); the
    other finds the order already handled and is a no-op. That, plus Razorpay
    retrying this webhook on anything but a 2xx response, is why every branch
    below ends by returning 200 once the request's signature has checked out
    - there is no tenant on the other end of this call to show an error to,
    and repeated retries for something already handled (or already logged
    for manual follow-up) would only add noise.

    Unlike every other route in this file, there is deliberately no auth
    dependency here - Razorpay can't send a bearer token. Trust instead comes
    entirely from the HMAC-SHA256 signature Razorpay computes over the exact
    raw request body using the webhook secret (Settings -> Online payments ->
    Razorpay Webhook Secret), verified below BEFORE the body is parsed or
    trusted for anything else.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    cfg = settings_service.get_all(db)
    webhook_secret = _razorpay_webhook_secret(cfg)
    if not webhook_secret:
        logger.error("Razorpay webhook received but no webhook secret is configured on this server.")
        raise HTTPException(503, detail="Webhook is not configured on this server.")

    client = _razorpay_client(cfg)
    try:
        client.utility.verify_webhook_signature(raw_body.decode("utf-8"), signature, webhook_secret)
    except razorpay.errors.SignatureVerificationError:
        logger.warning("Razorpay webhook signature verification failed - rejecting request.")
        raise HTTPException(400, detail="Invalid webhook signature.")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(400, detail="Malformed webhook payload.")

    event = payload.get("event", "")

    if event == "payment.captured":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        razorpay_payment_id = payment_entity.get("id")
        if not razorpay_order_id or not razorpay_payment_id:
            logger.warning("Razorpay webhook payment.captured missing order_id/payment_id: %s", payload)
            return {"success": True}

        order_row = (
            db.query(RazorpayOrder)
            .filter(RazorpayOrder.razorpay_order_id == razorpay_order_id)
            .with_for_update()
            .first()
        )
        if not order_row:
            # Most likely: an order created by some other integration on the
            # same Razorpay account, or a stale test event - not this app's
            # concern, but still a validly-signed request, so ack it.
            logger.warning(
                "Razorpay webhook payment.captured for unknown order %s (payment %s)",
                razorpay_order_id, razorpay_payment_id,
            )
            return {"success": True}

        if order_row.status != "created":
            # Already recorded - either verify() beat us to it, or Razorpay
            # is retrying a webhook delivery we already handled. Normal, not
            # an error.
            return {"success": True}

        try:
            _finalize_paid_razorpay_order(db, order_row, razorpay_payment_id, order_row.user_id)
            logger.info(
                "Razorpay webhook recorded payment %s for order %s", razorpay_payment_id, razorpay_order_id,
            )
        except _RazorpayNoPendingBillsError:
            db.rollback()
            logger.warning(
                "Razorpay webhook: payment %s (order %s) captured but no pending bills to apply it "
                "to - left as 'created' for manual reconciliation.", razorpay_payment_id, razorpay_order_id,
            )
        except Exception as exc:
            db.rollback()
            logger.error(
                "Razorpay webhook: payment %s (order %s) could not be recorded: %s",
                razorpay_payment_id, razorpay_order_id, exc,
            )

    elif event == "payment.failed":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        if razorpay_order_id:
            order_row = (
                db.query(RazorpayOrder)
                .filter(RazorpayOrder.razorpay_order_id == razorpay_order_id)
                .first()
            )
            if order_row and order_row.status == "created":
                order_row.status = "failed"
                db.commit()

    # Every other subscribed event (order.paid, refund.*, etc.) is
    # acknowledged but otherwise ignored - nothing in this app reads them
    # today. Always 2xx for a validly-signed request so Razorpay doesn't
    # keep retrying indefinitely.
    return {"success": True}
