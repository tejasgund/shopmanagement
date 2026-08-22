"""
routers/tenant_portal.py - Read-only tenant self-service endpoints that have
no dependency on Razorpay: profile, shops, bills, payments, deposit
payments, and the tenant's own financial summary.

Extracted verbatim from app.py (step 20 of the router/service split).

NOTE: tenant_home, and every Razorpay-related tenant route (create-order,
verify-payment, webhook, etc.) deliberately stay in app.py - they call
_razorpay_credentials/_razorpay_client, which read module-level constants
(RAZORPAY_KEY_ID_ENV etc.) that tests monkeypatch directly on the app
module. _tenant_payment_dict moved to domain_helpers.py rather than living
here, since tenant_home (staying in app.py) also needs it - putting it in a
router would force app.py to import from a router just for a helper.
"""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, DepositPayment, Payment, Shop, User, UserShop
from auth_service import require_tenant
from domain_helpers import _build_user_financial_summary, _decimal_to_float, _tenant_payment_dict
from schemas import UserResponse

router = APIRouter(tags=["Tenant"])


@router.get("/api/tenant/profile", response_model=UserResponse, tags=["Tenant"])
def tenant_profile(current_user: User = Depends(require_tenant)):
    """Return the authenticated tenant's own profile."""
    return current_user


@router.get("/api/tenant/shops", tags=["Tenant"])
def tenant_shops(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return all shops assigned to the authenticated tenant."""
    rows = (
        db.query(Shop, UserShop)
        .join(UserShop, UserShop.shop_id == Shop.id)
        .filter(UserShop.user_id == current_user.id)
        .all()
    )
    complexes = {c.id: c.name for c in db.query(Complex).all()}
    return [
        {
            "id":           s.id,
            "shop_number":  s.shop_number,
            "area_sqft":    _decimal_to_float(s.area_sqft),
            "status":       s.status,
            "complex_id":   s.complex_id,
            "complex_name": complexes.get(s.complex_id),
            "shop_rent":    _decimal_to_float(s.shop_rent),  # <-- DIRECT
            "shop_deposit": _decimal_to_float(s.shop_deposit),
            "agreement_start_date": us.agreement_start_date,
            "agreement_end_date": us.agreement_end_date,
        }
        for s, us in rows
    ]


@router.get("/api/tenant/bills", tags=["Tenant"])
def tenant_bills(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return all bills for the authenticated tenant."""
    bills = db.query(Bill).filter(Bill.user_id == current_user.id).order_by(Bill.id).all()
    return [
        {
            "id":             b.id,
            "shop_id":        b.shop_id,
            "bill_type":      b.bill_type,
            "description":    b.description,
            "amount":         _decimal_to_float(b.amount),
            "paid_amount":    _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "bill_date":      b.bill_date,
            "due_date":       b.due_date,
            "status":         b.status,
        }
        for b in bills
    ]


@router.get("/api/tenant/payments", tags=["Tenant"])
def tenant_payments(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return all payments made by the authenticated tenant (via their bills)."""
    # Join Payment → Bill → filter by user
    payments = (
        db.query(Payment)
        .join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == current_user.id)
        .order_by(Payment.id)
        .all()
    )
    return [_tenant_payment_dict(p) for p in payments]


@router.get("/api/tenant/deposit-payments", tags=["Tenant"])
def tenant_deposit_payments(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """Return all deposit payments made by the authenticated tenant."""
    deposits = (
        db.query(DepositPayment)
        .filter(DepositPayment.user_id == current_user.id)
        .order_by(DepositPayment.payment_date.desc())
        .all()
    )
    return [
        {
            "id": dp.id,
            "shop_id": dp.shop_id,
            "amount": _decimal_to_float(dp.amount),
            "payment_date": dp.payment_date,
            "remarks": dp.remarks,
        }
        for dp in deposits
    ]


@router.get("/api/tenant/financial-summary", tags=["Tenant"])
def tenant_financial_summary(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return the authenticated tenant's own full financial picture."""
    return _build_user_financial_summary(db, current_user)
