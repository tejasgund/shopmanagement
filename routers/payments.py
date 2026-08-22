"""
routers/payments.py - Payment CRUD, auto-allocate preview/confirm, update/
delete, and the paginated admin list.

Extracted verbatim from app.py (step 18 of the router/service split).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, Payment, Shop, User
from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import _decimal_to_float, _reconcile_bill
from schemas import (
    AllocationRow, AutoAllocateConfirmRequest, AutoAllocatePreviewRequest,
    AutoAllocatePreviewResponse, AutoAllocateResponse, AutoAllocateResult,
    PaymentCreate, PaymentResponse, PaymentUpdate,
)

router = APIRouter(tags=["Payment"])


@router.post("/api/payment", response_model=PaymentResponse, status_code=201, tags=["Payment"])
def record_payment(
    body:  PaymentCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """
    Record a payment against a bill.
    Automatically updates paid_amount, pending_amount, and status.
    Admin only.
    """
    bill = db.query(Bill).filter(Bill.id == body.bill_id).first()
    if not bill:
        raise HTTPException(404, detail="Bill not found")

    if bill.status == "paid":
        raise HTTPException(400, detail="Bill is already fully paid")

    pay = Payment(
        bill_id=body.bill_id,
        amount=Decimal(str(body.amount)),
        payment_method=body.payment_method,
        remarks=body.remarks,
        payment_date=body.payment_date or datetime.now(timezone.utc),
    )
    db.add(pay)
    db.flush()

    # Reload payments to include the new record before reconciling
    db.refresh(bill)
    _reconcile_bill(bill)

    write_audit(db, actor.id, "CREATE", "payments", pay.id, new_data=body.model_dump())
    db.commit()
    db.refresh(pay)
    return pay


@router.post("/api/payment/auto-allocate/preview", response_model=AutoAllocatePreviewResponse, tags=["Payment"])
def auto_allocate_preview(
    body: AutoAllocatePreviewRequest,
    db:   Session = Depends(get_db),
    _:    User    = Depends(require_admin),
):
    """
    Read-only. Shows the admin exactly which bills a lump-sum amount would be
    applied to (oldest due_date first) and how much each would get, WITHOUT
    creating any payment. Admin reviews/edits this on the frontend, then posts
    the (possibly adjusted) allocations to /confirm.
    """
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(400, detail="User not found")
    if body.shop_id is not None and not db.query(Shop).filter(Shop.id == body.shop_id).first():
        raise HTTPException(400, detail="Shop not found")

    q = db.query(Bill).filter(Bill.user_id == body.user_id, Bill.status.in_(["pending", "partial"]))
    if body.shop_id is not None:
        q = q.filter(Bill.shop_id == body.shop_id)
    bills = q.order_by(Bill.due_date.is_(None), Bill.due_date.asc(), Bill.bill_date.asc(), Bill.id.asc()).all()

    remaining = Decimal(str(body.amount)).quantize(Decimal("0.01"))
    rows = []
    for bill in bills:
        outstanding = Decimal(str(_decimal_to_float(bill.pending_amount))).quantize(Decimal("0.01"))
        if outstanding <= 0:
            continue
        alloc = min(remaining, outstanding) if remaining > 0 else Decimal("0")
        new_paid = _decimal_to_float(bill.paid_amount) + float(alloc)
        resulting_status = "paid" if new_paid >= _decimal_to_float(bill.amount) - 0.005 else ("partial" if new_paid > 0 else "pending")
        shop = db.query(Shop).filter(Shop.id == bill.shop_id).first()
        rows.append(AllocationRow(
            bill_id=bill.id, bill_type=bill.bill_type, description=bill.description,
            shop_number=shop.shop_number if shop else None, due_date=bill.due_date,
            bill_amount=_decimal_to_float(bill.amount), outstanding=float(outstanding),
            allocated=float(alloc), resulting_status=resulting_status,
        ))
        remaining -= alloc

    total_allocated = float(Decimal(str(body.amount)) - remaining)
    return AutoAllocatePreviewResponse(
        user_id=user.id, user_name=user.name, shop_id=body.shop_id, rows=rows,
        amount_received=body.amount, total_allocated=total_allocated, unallocated_amount=float(remaining),
    )


@router.post("/api/payment/auto-allocate/confirm", response_model=AutoAllocateResponse, status_code=201, tags=["Payment"])
def auto_allocate_confirm(
    body:  AutoAllocateConfirmRequest,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """
    Admin-approved execution step. Takes the (possibly hand-edited) list of
    {bill_id, amount} the admin reviewed on the preview screen and creates one
    normal Payment per row — identical to manual entry. Fully transactional
    (all-or-nothing) and row-locks each bill (SELECT ... FOR UPDATE) so a
    concurrent admin can't allocate against the same balance. Each row is
    re-validated against the bill's CURRENT outstanding balance at commit time
    (not the possibly-stale preview), and silently capped with a note if the
    balance shrank since the preview was shown.
    """
    if not db.query(User).filter(User.id == body.user_id).first():
        raise HTTPException(400, detail="User not found")

    bill_ids = [a.bill_id for a in body.allocations]
    if len(bill_ids) != len(set(bill_ids)):
        raise HTTPException(400, detail="Duplicate bill_id in allocations")

    try:
        bills = db.query(Bill).filter(Bill.id.in_(bill_ids)).with_for_update().all()
        bill_map = {b.id: b for b in bills}

        payments, allocations = [], []
        total_allocated = Decimal("0")

        for item in body.allocations:
            bill = bill_map.get(item.bill_id)
            if not bill or bill.user_id != body.user_id:
                raise HTTPException(400, detail=f"Bill {item.bill_id} not found for this tenant")

            outstanding = Decimal(str(_decimal_to_float(bill.pending_amount))).quantize(Decimal("0.01"))
            requested = Decimal(str(item.amount)).quantize(Decimal("0.01"))
            note = None
            if outstanding <= 0:
                continue  # already paid since preview — skip silently, nothing to allocate
            alloc = requested
            if requested > outstanding:
                alloc = outstanding
                note = f"Capped to current outstanding balance of {float(outstanding)} (balance changed since preview)"

            pay = Payment(bill_id=bill.id, amount=alloc, payment_method=body.payment_method, remarks=body.remarks,
                          payment_date=body.payment_date or datetime.now(timezone.utc))
            db.add(pay)
            db.flush()
            db.refresh(bill)
            _reconcile_bill(bill)

            write_audit(db, actor.id, "CREATE", "payments", pay.id, new_data={
                "bill_id": bill.id, "amount": float(alloc),
                "payment_method": body.payment_method, "auto_allocated": True, "admin_approved": True,
            })
            db.refresh(pay)
            payments.append(pay)
            allocations.append(AutoAllocateResult(bill_id=bill.id, allocated=float(alloc), bill_status=bill.status, note=note))
            total_allocated += alloc

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    unallocated = max(Decimal("0"), Decimal(str(body.amount_received)) - total_allocated)
    return AutoAllocateResponse(
        payments=payments, allocations=allocations,
        total_allocated=float(total_allocated), unallocated_amount=float(unallocated),
    )


@router.get("/api/payment", response_model=List[PaymentResponse], tags=["Payment"])
def list_payments(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all payments. Admin only."""
    return db.query(Payment).order_by(Payment.id).all()


@router.get("/api/payment/{payment_id}", tags=["Payment"])
def get_payment(
    payment_id: int,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """Return a single payment by ID, including its associated bill info. Admin only."""
    pay = db.query(Payment).filter(Payment.id == payment_id).first()
    if not pay:
        raise HTTPException(status_code=404, detail="Payment not found")
    bill = pay.bill
    return {
        "success": True,
        "data": {
            "id":             pay.id,
            "bill_id":        pay.bill_id,
            "amount":         _decimal_to_float(pay.amount),
            "payment_method": pay.payment_method,
            "payment_date":   pay.payment_date,
            "remarks":        pay.remarks,
            "created_at":     pay.created_at,
            "bill": {
                "id":        bill.id,
                "user_id":   bill.user_id,
                "shop_id":   bill.shop_id,
                "bill_type": bill.bill_type,
                "amount":    _decimal_to_float(bill.amount),
                "status":    bill.status,
            } if bill else None,
        },
    }


@router.put("/api/payment/{payment_id}", tags=["Payment"])
def update_payment(
    payment_id: int,
    body:       PaymentUpdate,
    db:         Session = Depends(get_db),
    actor:      User    = Depends(require_admin),
):
    """
    Partially update a payment.  After updating, the parent bill is
    automatically re-reconciled (paid_amount / pending_amount / status).

    Request body (all fields optional):
        amount         – positive float
        payment_method – string
        payment_date   – ISO-8601 datetime
        remarks        – string

    Admin only.
    """
    pay = db.query(Payment).filter(Payment.id == payment_id).first()
    if not pay:
        raise HTTPException(status_code=404, detail="Payment not found")

    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    old_snapshot = {
        "amount":         _decimal_to_float(pay.amount),
        "payment_method": pay.payment_method,
        "payment_date":   str(pay.payment_date),
        "remarks":        pay.remarks,
    }

    if "amount"         in changes: pay.amount         = Decimal(str(changes["amount"]))
    if "payment_method" in changes: pay.payment_method = changes["payment_method"]
    if "payment_date"   in changes: pay.payment_date   = changes["payment_date"]
    if "remarks"        in changes: pay.remarks        = changes["remarks"]

    db.flush()

    # Re-reconcile the parent bill
    bill = pay.bill
    if bill:
        db.refresh(bill)
        _reconcile_bill(bill)

    write_audit(db, actor.id, "UPDATE", "payments", pay.id,
                old_data=old_snapshot, new_data=changes)
    db.commit()
    db.refresh(pay)

    return {
        "success": True,
        "message": "Payment updated successfully",
        "data": {
            "id":             pay.id,
            "bill_id":        pay.bill_id,
            "amount":         _decimal_to_float(pay.amount),
            "payment_method": pay.payment_method,
            "payment_date":   pay.payment_date,
            "remarks":        pay.remarks,
        },
        "bill_reconciled": {
            "id":             bill.id,
            "paid_amount":    _decimal_to_float(bill.paid_amount),
            "pending_amount": _decimal_to_float(bill.pending_amount),
            "status":         bill.status,
        } if bill else None,
    }


@router.delete("/api/payment/{payment_id}", tags=["Payment"])
def delete_payment(
    payment_id: int,
    db:         Session = Depends(get_db),
    actor:      User    = Depends(require_admin),
):
    """
    Delete a payment and re-reconcile the parent bill.
    Admin only.
    """
    pay = db.query(Payment).filter(Payment.id == payment_id).first()
    if not pay:
        raise HTTPException(status_code=404, detail="Payment not found")

    bill = pay.bill
    old_snapshot = {
        "id":             pay.id,
        "bill_id":        pay.bill_id,
        "amount":         _decimal_to_float(pay.amount),
        "payment_method": pay.payment_method,
    }

    db.delete(pay)
    db.flush()

    bill_after = None
    if bill:
        db.refresh(bill)
        _reconcile_bill(bill)
        bill_after = {
            "id":             bill.id,
            "paid_amount":    _decimal_to_float(bill.paid_amount),
            "pending_amount": _decimal_to_float(bill.pending_amount),
            "status":         bill.status,
        }

    write_audit(db, actor.id, "DELETE", "payments", payment_id, old_data=old_snapshot)
    db.commit()

    return {
        "success": True,
        "message": f"Payment {payment_id} deleted and bill re-reconciled",
        "bill_reconciled": bill_after,
    }


@router.get("/api/payments", tags=["Payment"])
def list_payments_paginated(
    page:       int            = Query(1,  ge=1),
    limit:      int            = Query(20, ge=1, le=200),
    user_id:    Optional[int]  = None,
    bill_id:    Optional[int]  = None,
    shop_id:    Optional[int]  = None,
    complex_id: Optional[int]  = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    search:     Optional[str]  = None,
    db:         Session        = Depends(get_db),
    _:          User           = Depends(require_admin),
):
    """
    Paginated payments list enriched with tenant and shop info.
    Admin only.
    """
    q = (
        db.query(Payment, Bill, User, Shop)
        .join(Bill, Bill.id == Payment.bill_id)
        .join(User, User.id == Bill.user_id)
        .join(Shop, Shop.id == Bill.shop_id)
    )

    if user_id:    q = q.filter(Bill.user_id     == user_id)
    if bill_id:    q = q.filter(Payment.bill_id  == bill_id)
    if shop_id:    q = q.filter(Bill.shop_id     == shop_id)
    if complex_id: q = q.filter(Shop.complex_id  == complex_id)
    if start_date: q = q.filter(Payment.payment_date >= start_date)
    if end_date:   q = q.filter(Payment.payment_date <= end_date)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            User.name.ilike(term)
            | User.mobile.ilike(term)
            | Shop.shop_number.ilike(term)
            | Payment.payment_method.ilike(term)
            | Payment.remarks.ilike(term)
        )

    total  = q.count()
    offset = (page - 1) * limit
    rows   = q.order_by(Payment.payment_date.desc(), Payment.id.desc()).offset(offset).limit(limit).all()

    complexes = {c.id: c.name for c in db.query(Complex).all()}

    return {
        "success": True,
        "page":    page,
        "limit":   limit,
        "total":   total,
        "data": [
            {
                "id":             p.id,
                "payment_date":   p.payment_date,
                "amount":         _decimal_to_float(p.amount),
                "payment_method": p.payment_method,
                "remarks":        p.remarks,
                "created_at":     p.created_at,
                "bill": {
                    "id":        b.id,
                    "bill_type": b.bill_type,
                    "amount":    _decimal_to_float(b.amount),
                    "status":    b.status,
                },
                "user": {"id": u.id, "name": u.name, "mobile": u.mobile},
                "shop": {
                    "id":           s.id,
                    "shop_number":  s.shop_number,
                    "complex_id":   s.complex_id,
                    "complex_name": complexes.get(s.complex_id),
                },
            }
            for p, b, u, s in rows
        ],
    }
