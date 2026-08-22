"""
routers/ledger.py - GET /api/ledger/monthly (Admin) and
GET /api/tenant/ledger/monthly (tenant self-service), both backed by the
shared _get_monthly_ledger_data() builder.

Extracted verbatim from app.py (step 7 of the router/service split).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import extract
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, DepositPayment, Payment, Shop, User, UserShop
from auth_service import require_admin, require_tenant
from domain_helpers import _decimal_to_float

router = APIRouter()


@router.get("/api/ledger/monthly", tags=["Ledger"])
def monthly_ledger(
    user_id: int,
    year: int = Query(..., description="Year to filter bills by bill_date"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    return _get_monthly_ledger_data(user_id, year, db)


def _get_monthly_ledger_data(user_id: int, year: int, db: Session) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")

    bills = db.query(Bill).filter(
        Bill.user_id == user_id,
        extract('year', Bill.bill_date) == year
    ).all()

    # Payments are grouped by the month money actually changed hands (payment_date),
    # not by the month of the bill they were applied against — a payment made in
    # August against a July bill must show up under August so the tenant can see
    # how much they actually paid in that month.
    payments = db.query(Payment).join(Bill, Bill.id == Payment.bill_id).filter(
        Bill.user_id == user_id,
        extract('year', Payment.payment_date) == year
    ).all()

    monthly_data = {m: {"bills": [], "billed": 0.0, "paid": 0.0, "remaining": 0.0} for m in range(1,13)}
    for bill in bills:
        month = bill.bill_date.month
        monthly_data[month]["bills"].append(bill)
        monthly_data[month]["billed"] += _decimal_to_float(bill.amount)
        monthly_data[month]["remaining"] += _decimal_to_float(bill.pending_amount)

    for payment in payments:
        month = payment.payment_date.month
        monthly_data[month]["paid"] += _decimal_to_float(payment.amount)

    monthly_rows = []
    total_billed = total_paid = total_remaining = 0.0
    total_bills_count = 0
    for m in range(1,13):
        data = monthly_data[m]
        count = len(data["bills"])
        billed = round(data["billed"], 2)
        paid = round(data["paid"], 2)
        remaining = round(data["remaining"], 2)
        total_billed += billed
        total_paid += paid
        total_remaining += remaining
        total_bills_count += count

        # Status reflects whether that month's bills are settled, based on the
        # bills' own running balance — independent of which month the payment
        # landed in (that's what "paid" above is for).
        if count == 0:
            status = "Paid" if paid > 0 else "No bills"
        elif remaining == 0:
            status = "Paid"
        elif remaining < billed:
            status = "Partial"
        else:
            status = "Pending"

        monthly_rows.append({
            "month": m,
            "bills_count": count,
            "billed": billed,
            "paid": paid,
            "remaining": remaining,
            "status": status
        })

    shops = db.query(Shop).join(UserShop, UserShop.shop_id == Shop.id).filter(UserShop.user_id == user_id).all()
    complexes = {c.id: c for c in db.query(Complex).all()}
    shop_list = [{
        "id": s.id,
        "shop_number": s.shop_number,
        "complex_id": s.complex_id,
        "complex_name": complexes.get(s.complex_id).name if s.complex_id and complexes.get(s.complex_id) else None,
        "area_sqft": _decimal_to_float(s.area_sqft),
        "shop_rent": _decimal_to_float(s.shop_rent),
        "shop_deposit": _decimal_to_float(s.shop_deposit),
    } for s in shops]

    payment_list = [{
        "id": p.id,
        "amount": _decimal_to_float(p.amount),
        "payment_method": p.payment_method,
        "payment_date": p.payment_date,
    } for p in payments]

    deposits = db.query(DepositPayment).filter(
        DepositPayment.user_id == user_id,
        extract('year', DepositPayment.payment_date) == year
    ).all()
    deposit_list = [{
        "id": dp.id,
        "amount": _decimal_to_float(dp.amount),
        "payment_date": dp.payment_date,
        "remarks": dp.remarks,
    } for dp in deposits]

    total_deposit_paid = sum(
        _decimal_to_float(dp.amount)
        for dp in db.query(DepositPayment).filter(DepositPayment.user_id == user_id).all()
    )

    return {
        "tenant": {"id": user.id, "name": user.name, "mobile": user.mobile, "email": user.email},
        "summary": {
            "outstanding_dues": round(total_remaining, 2),
            "total_billed": round(total_billed, 2),
            "total_paid": round(total_paid, 2),
            "deposit_on_file": round(total_deposit_paid, 2),
        },
        "monthly": monthly_rows,
        "shops": shop_list,
        "bills": [{
            "id": b.id,
            "bill_type": b.bill_type,
            "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "status": b.status,
            "bill_date": b.bill_date,
            "due_date": b.due_date,
        } for b in bills],
        "payments": payment_list,
        "deposits": deposit_list,
    }


@router.get("/api/tenant/ledger/monthly", tags=["Tenant"])
def tenant_monthly_ledger(
    year: int = Query(..., description="Year to filter bills by bill_date"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    return _get_monthly_ledger_data(current_user.id, year, db)
