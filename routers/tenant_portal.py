"""
routers/tenant_portal.py - Read-only tenant self-service endpoints: profile,
shops, bills, payments, deposit payments, the tenant's own financial
summary, and the combined tenant_home bundle. Razorpay-related tenant routes
(create-order, verify-payment, webhook) live in routers/razorpay.py instead,
since they depend on razorpay_service.py rather than anything here.

Extracted verbatim from app.py (step 20 of the router/service split;
tenant_home joined it in step 23). _tenant_payment_dict lives in
domain_helpers.py rather than here, shared as-is from before this file
existed.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, DepositPayment, Meter, MeterReading, Payment, Shop, User, UserShop
from auth_service import require_tenant
from domain_helpers import _build_user_financial_summary, _decimal_to_float, _tenant_payment_dict
from meter_helpers import _reading_to_dict, _tenant_shop_ids
from razorpay_service import _razorpay_public_config
from schemas import UserResponse
import meter_service
import settings_service

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


@router.get("/api/tenant/home", tags=["Tenant"])
def tenant_home(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """
    Everything the tenant portal needs on open, in one response.

    The portal previously made seven parallel calls on every load. On a shop's
    phone connection each round trip costs more than the query does, so this
    collapses them into one. Same data, same shapes as the individual
    endpoints - those are all still there and still work, so nothing that
    already calls them breaks.
    """
    shop_ids = _tenant_shop_ids(db, current_user.id)

    # ── Shops (with agreement dates and rent) ──
    shops = []
    if shop_ids:
        rows = (
            db.query(Shop, UserShop)
            .join(UserShop, UserShop.shop_id == Shop.id)
            .filter(UserShop.user_id == current_user.id)
            .all()
        )
        complexes = {c.id: c.name for c in db.query(Complex).all()}
        shops = [
            {
                "id": s.id,
                "shop_number": s.shop_number,
                "complex_id": s.complex_id,
                "complex_name": complexes.get(s.complex_id),
                "area_sqft": _decimal_to_float(s.area_sqft),
                "shop_rent": _decimal_to_float(s.shop_rent),
                "shop_deposit": _decimal_to_float(s.shop_deposit),
                "agreement_start_date": us.agreement_start_date,
                "agreement_end_date": us.agreement_end_date,
            }
            for s, us in rows
        ]

    # ── Bills ──
    bills = db.query(Bill).filter(Bill.user_id == current_user.id).order_by(Bill.id).all()
    bills_out = [
        {
            "id": b.id, "shop_id": b.shop_id, "bill_type": b.bill_type,
            "description": b.description,
            "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "bill_date": b.bill_date, "due_date": b.due_date, "status": b.status,
        }
        for b in bills
    ]

    # ── Payments ──
    payments = (
        db.query(Payment)
        .join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == current_user.id)
        .order_by(Payment.id)
        .all()
    )

    # ── Deposit payments ──
    deposits = (
        db.query(DepositPayment)
        .filter(DepositPayment.user_id == current_user.id)
        .order_by(DepositPayment.payment_date.desc())
        .all()
    )

    # ── Meters + readings ──
    meters_out, readings_out = [], []
    if shop_ids:
        meters = (
            db.query(Meter)
            .filter(Meter.shop_id.in_(shop_ids), Meter.is_active == True)
            .order_by(Meter.id)
            .all()
        )
        shop_numbers = {s["id"]: s["shop_number"] for s in shops}
        for m in meters:
            pending = (
                db.query(MeterReading)
                .filter(
                    MeterReading.meter_id == m.id,
                    MeterReading.user_id == current_user.id,
                    MeterReading.status == "pending",
                )
                .order_by(MeterReading.id.desc())
                .first()
            )
            meters_out.append({
                "id": m.id,
                "meter_number": m.meter_number,
                "meter_type": m.meter_type,
                "shop_id": m.shop_id,
                "shop_number": shop_numbers.get(m.shop_id),
                "previous_reading": float(meter_service.previous_reading_value(db, m)),
                "pending_reading_id": pending.id if pending else None,
                "has_pending": pending is not None,
            })

        readings = (
            db.query(MeterReading)
            .filter(MeterReading.user_id == current_user.id)
            .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
            .limit(24)          # the portal only ever shows the recent ones
            .all()
        )
        readings_out = [_reading_to_dict(db, r) for r in readings]

    # ── Branding / payment-methods line ──
    cfg = settings_service.get_all(db)
    razorpay_enabled, razorpay_key_id = _razorpay_public_config(cfg)

    return {
        "profile": {
            "id": current_user.id, "name": current_user.name,
            "mobile": current_user.mobile, "email": current_user.email,
            "role": current_user.role,
        },
        "shops": shops,
        "bills": bills_out,
        "payments": [_tenant_payment_dict(p) for p in payments],
        "deposits": [
            {
                "id": dp.id, "shop_id": dp.shop_id,
                "amount": _decimal_to_float(dp.amount),
                "payment_date": dp.payment_date, "remarks": dp.remarks,
            }
            for dp in deposits
        ],
        "meters": meters_out,
        "readings": readings_out,
        "settings": {
            "app_name": cfg.get("app.name"),
            "currency_symbol": cfg.get("app.currency_symbol"),
            "support_contact": cfg.get("app.support_contact"),
            "payment_methods": cfg.get("app.payment_methods"),
            "razorpay_enabled": razorpay_enabled,
            "razorpay_key_id": razorpay_key_id,
        },
    }
