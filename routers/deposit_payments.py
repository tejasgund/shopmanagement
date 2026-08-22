"""
routers/deposit_payments.py - Deposit Payment CRUD (Admin only).

Extracted verbatim from app.py (step 19 of the router/service split).

GET /api/deposit-payments (plural, paginated) was added later, in the
pagination pass - additive alongside the original GET /api/deposit-payment
(singular, unbounded), same page/limit/total convention already used by
GET /api/bills and GET /api/payments next to their unpaginated siblings.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Complex, DepositPayment, Shop, User, UserShop
from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import _decimal_to_float, _deposit_paid_for_shop
from schemas import DepositPaymentCreate, DepositPaymentResponse, DepositPaymentUpdate

router = APIRouter(tags=["Deposit Payment"])


@router.post("/api/deposit-payment", response_model=DepositPaymentResponse, status_code=201, tags=["Deposit Payment"])
def create_deposit_payment(
    body:  DepositPaymentCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Record a deposit payment for a tenant against a specific shop. Admin only."""
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    shop = db.query(Shop).filter(Shop.id == body.shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")

    user_shop = db.query(UserShop).filter(
        UserShop.user_id == body.user_id, UserShop.shop_id == body.shop_id
    ).first()
    if not user_shop:
        raise HTTPException(400, detail="Shop not assigned to this user")

    already_paid = _deposit_paid_for_shop(db, body.user_id, body.shop_id)
    deposit_required = _decimal_to_float(shop.shop_deposit)
    if already_paid + body.amount > deposit_required:
        raise HTTPException(400, detail=(
            f"Amount exceeds remaining deposit. Required={deposit_required}, "
            f"already paid={already_paid}, remaining={max(0.0, deposit_required - already_paid)}"
        ))

    dp = DepositPayment(
        user_id      = body.user_id,
        shop_id      = body.shop_id,
        amount       = Decimal(str(body.amount)),
        payment_date = body.payment_date or datetime.now(timezone.utc),
        remarks      = body.remarks,
    )
    db.add(dp)
    db.flush()
    write_audit(db, actor.id, "CREATE", "deposit_payments", dp.id, new_data=body.model_dump())
    db.commit()
    db.refresh(dp)
    return {
        "id": dp.id, "user_id": dp.user_id, "shop_id": dp.shop_id, "shop_number": shop.shop_number,
        "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
        "remarks": dp.remarks, "created_at": dp.created_at,
    }


@router.get("/api/deposit-payment", tags=["Deposit Payment"])
def list_deposit_payments(
    user_id:    Optional[int] = None,
    shop_id:    Optional[int] = None,
    complex_id: Optional[int] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """List all deposit payments. Supports filters by user_id, shop_id, complex_id. Admin only."""
    q = db.query(DepositPayment, User, Shop).join(User, User.id == DepositPayment.user_id).join(Shop, Shop.id == DepositPayment.shop_id)
    if user_id is not None:
        q = q.filter(DepositPayment.user_id == user_id)
    if shop_id is not None:
        q = q.filter(DepositPayment.shop_id == shop_id)
    if complex_id is not None:
        q = q.filter(Shop.complex_id == complex_id)

    complexes = {c.id: c.name for c in db.query(Complex).all()}
    rows = q.order_by(DepositPayment.id).all()
    return [
        {
            "id": dp.id, "user_id": dp.user_id, "user_name": u.name,
            "shop_id": dp.shop_id, "shop_number": s.shop_number,
            "complex_id": s.complex_id, "complex_name": complexes.get(s.complex_id),
            "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
            "remarks": dp.remarks, "created_at": dp.created_at,
        }
        for dp, u, s in rows
    ]


@router.get("/api/deposit-payments", tags=["Deposit Payment"])
def list_deposit_payments_paginated(
    page:       int            = Query(1,  ge=1),
    limit:      int            = Query(20, ge=1, le=200),
    user_id:    Optional[int]  = None,
    shop_id:    Optional[int]  = None,
    complex_id: Optional[int]  = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    search:     Optional[str]  = None,
    db:         Session        = Depends(get_db),
    _:          User           = Depends(require_admin),
):
    """
    Paginated deposit-payments list enriched with tenant and shop info.
    Additive alongside GET /api/deposit-payment (which returns everything,
    unbounded) - same page/limit/total convention as GET /api/bills and
    GET /api/payments. Admin only.
    """
    q = (
        db.query(DepositPayment, User, Shop)
        .join(User, User.id == DepositPayment.user_id)
        .join(Shop, Shop.id == DepositPayment.shop_id)
    )

    if user_id is not None:    q = q.filter(DepositPayment.user_id == user_id)
    if shop_id is not None:    q = q.filter(DepositPayment.shop_id == shop_id)
    if complex_id is not None: q = q.filter(Shop.complex_id == complex_id)
    if start_date is not None: q = q.filter(DepositPayment.payment_date >= start_date)
    if end_date is not None:   q = q.filter(DepositPayment.payment_date <= end_date)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            User.name.ilike(term)
            | User.mobile.ilike(term)
            | Shop.shop_number.ilike(term)
            | DepositPayment.remarks.ilike(term)
        )

    total  = q.count()
    offset = (page - 1) * limit
    rows   = (
        q.order_by(DepositPayment.payment_date.desc(), DepositPayment.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    complexes = {c.id: c.name for c in db.query(Complex).all()}

    return {
        "success": True,
        "page":    page,
        "limit":   limit,
        "total":   total,
        "data": [
            {
                "id": dp.id, "user_id": dp.user_id, "user_name": u.name,
                "shop_id": dp.shop_id, "shop_number": s.shop_number,
                "complex_id": s.complex_id, "complex_name": complexes.get(s.complex_id),
                "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
                "remarks": dp.remarks, "created_at": dp.created_at,
            }
            for dp, u, s in rows
        ],
    }


@router.get("/api/deposit-payment/{dp_id}", tags=["Deposit Payment"])
def get_deposit_payment(
    dp_id: int,
    db:    Session = Depends(get_db),
    _:     User    = Depends(require_admin),
):
    """Return a single deposit payment with shop + user context. Admin only."""
    dp = db.query(DepositPayment).filter(DepositPayment.id == dp_id).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Deposit payment not found")
    user = db.query(User).filter(User.id == dp.user_id).first()
    shop = db.query(Shop).filter(Shop.id == dp.shop_id).first()
    return {
        "success": True,
        "data": {
            "id":           dp.id,
            "user_id":      dp.user_id,
            "user_name":    user.name   if user else None,
            "shop_id":      dp.shop_id,
            "shop_number":  shop.shop_number if shop else None,
            "amount":       _decimal_to_float(dp.amount),
            "payment_date": dp.payment_date,
            "remarks":      dp.remarks,
            "created_at":   dp.created_at,
        },
    }


@router.put("/api/deposit-payment/{dp_id}", tags=["Deposit Payment"])
def update_deposit_payment(
    dp_id: int,
    body:  DepositPaymentUpdate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """
    Partially update a deposit payment.

    Business rule: the total deposit paid for this tenant/shop (including this
    record after the update) must not exceed the shop's required deposit.

    Request body (all optional):
        amount       – positive float
        payment_date – ISO-8601 datetime
        remarks      – string

    Admin only.
    """
    dp = db.query(DepositPayment).filter(DepositPayment.id == dp_id).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Deposit payment not found")

    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    old_snapshot = {
        "amount":       _decimal_to_float(dp.amount),
        "payment_date": str(dp.payment_date),
        "remarks":      dp.remarks,
    }

    if "amount" in changes:
        shop = db.query(Shop).filter(Shop.id == dp.shop_id).first()
        deposit_required = _decimal_to_float(shop.shop_deposit) if shop else 0.0
        # total paid EXCLUDING this record, then re-add with new amount
        other_paid = _deposit_paid_for_shop(db, dp.user_id, dp.shop_id) - _decimal_to_float(dp.amount)
        if other_paid + changes["amount"] > deposit_required:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Updated amount would exceed the required deposit. "
                    f"Required={deposit_required}, other payments={round(other_paid,2)}, "
                    f"max you can set here={round(max(0, deposit_required - other_paid), 2)}"
                ),
            )
        dp.amount = Decimal(str(changes["amount"]))

    if "payment_date" in changes: dp.payment_date = changes["payment_date"]
    if "remarks"      in changes: dp.remarks      = changes["remarks"]

    write_audit(db, actor.id, "UPDATE", "deposit_payments", dp.id,
                old_data=old_snapshot, new_data=changes)
    db.commit()
    db.refresh(dp)

    shop = db.query(Shop).filter(Shop.id == dp.shop_id).first()
    return {
        "success": True,
        "message": "Deposit payment updated successfully",
        "data": {
            "id":           dp.id,
            "user_id":      dp.user_id,
            "shop_id":      dp.shop_id,
            "shop_number":  shop.shop_number if shop else None,
            "amount":       _decimal_to_float(dp.amount),
            "payment_date": dp.payment_date,
            "remarks":      dp.remarks,
        },
    }


@router.delete("/api/deposit-payment/{dp_id}", tags=["Deposit Payment"])
def delete_deposit_payment(
    dp_id: int,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Delete a deposit payment record. Admin only."""
    dp = db.query(DepositPayment).filter(DepositPayment.id == dp_id).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Deposit payment not found")

    old_snapshot = {
        "id":      dp.id, "user_id": dp.user_id, "shop_id": dp.shop_id,
        "amount":  _decimal_to_float(dp.amount), "payment_date": str(dp.payment_date),
    }
    db.delete(dp)
    write_audit(db, actor.id, "DELETE", "deposit_payments", dp_id, old_data=old_snapshot)
    db.commit()
    return {"success": True, "message": f"Deposit payment {dp_id} deleted"}
