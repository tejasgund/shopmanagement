"""
helpers/domain.py - shared, stateless helpers used across many routes:
decimal formatting, shop-owner lookups, bill reconciliation, and the tenant
financial-summary builder shared by the admin and tenant portals.

Extracted verbatim from app.py's UTILITY section (step 4 of the router/
service split). _razorpay_credentials / _razorpay_webhook_secret /
_razorpay_public_config, which also lived in the same UTILITY section, moved
instead to services/razorpay.py (step 22) alongside the rest of the Razorpay
integration - see that module's docstring.
"""

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.schema import Bill, Complex, DepositPayment, Payment, Shop, User, UserShop
from schemas.api import ShopOwnerInfo, ShopResponse


def _decimal_to_float(value) -> float:
    """Convert Decimal to float for Pydantic serialisation."""
    return float(value) if isinstance(value, Decimal) else (value or 0.0)


def _shop_owner_map(db: Session, shop_ids: Optional[List[int]] = None) -> dict:
    """
    Build {shop_id: ShopOwnerInfo} for current owners.
    Since one shop should have at most one active owner, this takes the
    most recently assigned UserShop row per shop as the "current" owner.
    """
    q = db.query(UserShop, User).join(User, User.id == UserShop.user_id)
    if shop_ids is not None:
        q = q.filter(UserShop.shop_id.in_(shop_ids))
    rows = q.order_by(UserShop.shop_id, UserShop.assigned_at.desc()).all()

    owner_map = {}
    for user_shop, user in rows:
        if user_shop.shop_id not in owner_map:  # first row per shop = most recent
            owner_map[user_shop.shop_id] = ShopOwnerInfo(
                id=user.id, name=user.name, mobile=user.mobile,
                agreement_start_date=user_shop.agreement_start_date,
                agreement_end_date=user_shop.agreement_end_date,
            )
    return owner_map


def _shop_to_response(shop: Shop, owner_map: dict) -> ShopResponse:
    data = ShopResponse.model_validate(shop)
    data.assigned_to = owner_map.get(shop.id)
    return data


def bill_payable(bill: Bill) -> float:
    """
    What is actually owed on a bill: the original amount plus any late penalty.

    `bill.amount` is deliberately never touched by the penalty task, so it
    always answers "what was this bill for". This function answers the
    different question "what must be paid", and is the only place the two are
    added together.
    """
    return _decimal_to_float(bill.amount) + _decimal_to_float(bill.penalty_amount)


def _reconcile_bill(bill: Bill):
    """
    Recompute paid_amount, pending_amount, and status from linked payments.

    The single place in the app that decides what a bill owes - which is why
    adding the penalty here is enough for every payment route (cash,
    auto-allocate, Razorpay) to charge it without any of them changing: they
    all end up back in this function.
    """
    total_paid = sum(_decimal_to_float(p.amount) for p in bill.payments)
    payable    = bill_payable(bill)
    bill.paid_amount    = Decimal(str(total_paid))
    bill.pending_amount = Decimal(str(max(0.0, payable - total_paid)))

    if total_paid <= 0:
        bill.status = "pending"
    elif total_paid >= payable:
        bill.status = "paid"
    else:
        bill.status = "partial"


def bill_penalty_dict(bill: Bill) -> dict:
    """
    The penalty breakdown for one bill, in the shape both dashboards show.

    Every figure the tenant needs to understand why the amount went up, in one
    place so the admin screen and the tenant portal can never explain the same
    bill differently. `days_overdue` is the plain calendar count since the due
    date; `penalty_days` is what was actually charged for, which is lower when
    a grace period applies.
    """
    original = _decimal_to_float(bill.amount)
    penalty = _decimal_to_float(bill.penalty_amount)
    days_overdue = 0
    if bill.due_date and bill.status != "paid":
        from datetime import datetime as _dt
        days_overdue = max(0, (_dt.now().date() - bill.due_date.date()).days)

    return {
        "original_amount": round(original, 2),
        "penalty_amount": round(penalty, 2),
        "penalty_days": bill.penalty_days or 0,
        "days_overdue": days_overdue,
        "penalty_charged_through": bill.penalty_charged_through,
        "total_payable": round(original + penalty, 2),
        "has_penalty": penalty > 0,
    }


def _tenant_payment_dict(p: Payment) -> dict:
    """
    One payment row as the tenant portal needs it.

    created_at matters more than it looks: when a lump sum is split across
    several bills by auto-allocate, every row it creates is written in the
    same transaction, so they share a created_at to the second. The portal
    uses that to show "you paid Rs 6,000" once instead of three fragments,
    which is what tenants were ringing up about. payment_group is used in
    preference when it exists (see the note in tenant-payments.js).

    Shared by /api/tenant/payments (routers/tenant_portal.py) and
    tenant_home (app.py) - moved here (step 20 of the router/service split)
    so both can use the exact same shape without app.py importing from a
    router.
    """
    return {
        "id":             p.id,
        "bill_id":        p.bill_id,
        "amount":         _decimal_to_float(p.amount),
        "payment_method": p.payment_method,
        "payment_date":   p.payment_date,
        "remarks":        p.remarks,
        "created_at":     p.created_at,
        "payment_group":  getattr(p, "payment_group", None),
        "razorpay_payment_id": p.razorpay_payment_id,
    }


def _current_user_shops(db: Session, user_id: int) -> List[UserShop]:
    """All current UserShop assignment rows for a given user."""
    return db.query(UserShop).filter(UserShop.user_id == user_id).all()


def _deposit_paid_for_shop(db: Session, user_id: int, shop_id: int) -> float:
    rows = db.query(DepositPayment).filter(
        DepositPayment.user_id == user_id, DepositPayment.shop_id == shop_id
    ).all()
    return sum(_decimal_to_float(r.amount) for r in rows)


def _pending_rent_for_user(db: Session, user_id: int) -> float:
    """Sum of pending_amount across all non-paid Rent bills for a user."""
    rows = (
        db.query(Bill)
        .filter(Bill.user_id == user_id, Bill.bill_type == "Rent", Bill.status != "paid")
        .all()
    )
    return sum(_decimal_to_float(b.pending_amount) for b in rows)


def _build_user_financial_summary(db: Session, user: User) -> dict:
    """
    Shared logic for /api/user/{id}/financial-summary and
    /api/tenant/financial-summary (self), since both need the same shape.
    """
    user_shops = _current_user_shops(db, user.id)
    shop_ids = [us.shop_id for us in user_shops]
    shops = {s.id: s for s in db.query(Shop).filter(Shop.id.in_(shop_ids)).all()} if shop_ids else {}
    complexes = {c.id: c for c in db.query(Complex).all()}

    # Bulk-fetch deposit-paid sums per shop for this user instead of one
    # query per shop - same numbers as before, computed once.
    deposit_paid_by_shop = {}
    if shop_ids:
        for sid, total in (
            db.query(DepositPayment.shop_id, func.sum(DepositPayment.amount))
            .filter(DepositPayment.user_id == user.id, DepositPayment.shop_id.in_(shop_ids))
            .group_by(DepositPayment.shop_id)
            .all()
        ):
            deposit_paid_by_shop[sid] = _decimal_to_float(total)

    shops_summary_list = []
    total_monthly_rent = 0.0
    total_deposit_required = 0.0
    total_deposit_paid = 0.0

    for us in user_shops:
        shop = shops.get(us.shop_id)
        if not shop:
            continue
        rent = _decimal_to_float(shop.shop_rent)  # <-- DIRECT USE
        deposit_required = _decimal_to_float(shop.shop_deposit)
        deposit_paid = deposit_paid_by_shop.get(shop.id, 0.0)

        total_monthly_rent += rent
        total_deposit_required += deposit_required
        total_deposit_paid += deposit_paid

        shops_summary_list.append({
            "id": shop.id,
            "shop_number": shop.shop_number,
            "complex_id": shop.complex_id,
            "complex_name": complexes.get(shop.complex_id).name if shop.complex_id and complexes.get(
                shop.complex_id) else None,
            "area_sqft": _decimal_to_float(shop.area_sqft),
            "shop_rent": rent,
            "shop_deposit": deposit_required,
            "status": shop.status,
            "agreement_start_date": us.agreement_start_date,
            "agreement_end_date": us.agreement_end_date,
        })

    total_pending_rent = _pending_rent_for_user(db, user.id)
    total_rent_collected_or_paid = sum(
        _decimal_to_float(b.paid_amount)
        for b in db.query(Bill).filter(Bill.user_id == user.id, Bill.bill_type == "Rent").all()
    )

    bills = db.query(Bill).filter(Bill.user_id == user.id).order_by(Bill.id).all()
    shop_numbers = {s.id: s.shop_number for s in shops.values()}

    bills_list = [
        {
            "id": b.id, "shop_id": b.shop_id, "shop_number": shop_numbers.get(b.shop_id),
            "bill_type": b.bill_type, "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount), "pending_amount": _decimal_to_float(b.pending_amount),
            "status": b.status, "bill_date": b.bill_date, "due_date": b.due_date,
            "description": b.description,
        }
        for b in bills
    ]

    payments = (
        db.query(Payment).join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == user.id).order_by(Payment.id).all()
    )
    payment_history = [
        {
            "id": p.id, "bill_id": p.bill_id, "shop_id": p.bill.shop_id,
            "shop_number": shop_numbers.get(p.bill.shop_id), "bill_type": p.bill.bill_type,
            "amount": _decimal_to_float(p.amount), "payment_method": p.payment_method,
            "payment_date": p.payment_date, "remarks": p.remarks,
        }
        for p in payments
    ]

    deposit_payments = (
        db.query(DepositPayment).filter(DepositPayment.user_id == user.id).order_by(DepositPayment.id).all()
    )
    deposit_payment_history = [
        {
            "id": dp.id, "shop_id": dp.shop_id, "shop_number": shop_numbers.get(dp.shop_id),
            "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
            "remarks": dp.remarks,
        }
        for dp in deposit_payments
    ]

    return {
        "user": {
            "id": user.id, "name": user.name, "mobile": user.mobile,
            "email": user.email, "role": user.role, "is_active": user.is_active,
        },
        "shops_summary": {"total_shops": len(shops_summary_list), "shops": shops_summary_list},
        "shops": shops_summary_list,  # convenience alias for tenant self-summary consumers
        "rent_summary": {
            "total_monthly_rent": round(total_monthly_rent, 2),
            "total_pending_rent": round(total_pending_rent, 2),
            "total_rent_collected": round(total_rent_collected_or_paid, 2),
            "total_rent_paid": round(total_rent_collected_or_paid, 2),
        },
        "deposit_summary": {
            "total_deposit_required": round(total_deposit_required, 2),
            "total_deposit_paid": round(total_deposit_paid, 2),
            "remaining_deposit": round(total_deposit_required - total_deposit_paid, 2),
        },
        "outstanding_balance": round(total_pending_rent, 2),
        "bills": bills_list,
        "payment_history": payment_history,
        "deposit_payment_history": deposit_payment_history,
    }
