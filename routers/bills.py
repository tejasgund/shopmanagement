"""
routers/bills.py - Bill CRUD, update/delete, and the paginated admin list.

Extracted verbatim from app.py (step 17 of the router/service split).

NOTE: POST /api/bills/generate-rent and the whole rent-bill-generation
subsystem deliberately stay in app.py, not here - generate_rent_bills_for_date
is monkeypatched directly on app_module by tests, and the nightly scheduler
that calls it lives right next to it. This router only covers plain bill
CRUD, which has no dependency on that subsystem.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, Payment, Shop, User, UserShop
from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import _decimal_to_float, _reconcile_bill
from schemas import BillCreate, BillResponse, BillUpdate

router = APIRouter(tags=["Bill"])


@router.post("/api/bill", response_model=BillResponse, status_code=201, tags=["Bill"])
def create_bill(
    body:  BillCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """
    Create a bill for a tenant.

    bill_type == "Rent": the bill amount is auto-filled from the current shop rent.
    Any `amount` supplied in the request body is ignored for Rent bills — the server
    is the source of truth so rent always matches what is set on the shop.

    Any other bill_type (e.g. "Electricity", "Maintenance", "Other"):
    `amount` is required and used as-is. `description` is optional and is
    commonly used to clarify what the charge is for.
    Admin only.
    """
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(400, detail="User not found")
    shop = db.query(Shop).filter(Shop.id == body.shop_id).first()
    if not shop:
        raise HTTPException(400, detail="Shop not found")

    is_rent = body.bill_type.strip().lower() == "rent"

    if is_rent:
        user_shop = db.query(UserShop).filter(
            UserShop.user_id == body.user_id, UserShop.shop_id == body.shop_id
        ).first()
        if not user_shop:
            raise HTTPException(400, detail="This shop is not assigned to this user; cannot auto-fill rent.")
        # Use the current shop rent directly
        amount_value = _decimal_to_float(shop.shop_rent)
        if amount_value <= 0:
            raise HTTPException(400, detail="Shop rent is not configured (0 or negative).")
    else:
        if body.amount is None or body.amount <= 0:
            raise HTTPException(400, detail="amount is required and must be greater than 0 for non-Rent bill types.")
        amount_value = body.amount

    amount = Decimal(str(amount_value))
    bill = Bill(
        user_id        = body.user_id,
        shop_id        = body.shop_id,
        bill_type      = body.bill_type,
        description    = body.description,
        amount         = amount,
        paid_amount    = Decimal("0"),
        pending_amount = amount,
        due_date       = body.due_date,
        bill_date=body.bill_date or datetime.now(timezone.utc),
        status         = "pending",
    )
    db.add(bill)
    db.flush()

    write_audit(db, actor.id, "CREATE", "bills", bill.id, new_data={**body.model_dump(), "amount": float(amount)})
    db.commit()
    db.refresh(bill)
    return bill


@router.get("/api/bill", response_model=List[BillResponse], tags=["Bill"])
def list_bills(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all bills. Admin only."""
    return db.query(Bill).order_by(Bill.id).all()


@router.get("/api/bill/{id}", response_model=BillResponse, tags=["Bill"])
def get_bill(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single bill. Admin only."""
    obj = db.query(Bill).filter(Bill.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Bill not found")
    return obj


@router.put("/api/bill/{bill_id}", tags=["Bill"])
def update_bill(
    bill_id: int,
    body:    BillUpdate,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Partially update a bill.  Only the fields included in the request body
    are changed.

    Notes:
    - Changing `amount` on a bill that already has payments automatically
      re-reconciles paid_amount / pending_amount / status.
    - Setting status to 'cancelled' is allowed; the bill's pending amount
      is forced to 0.
    - You cannot lower `amount` below what has already been paid unless you
      first delete the excess payments.

    Admin only.

    Request body (all fields optional):
        bill_type   – string
        description – string
        amount      – positive float
        due_date    – ISO-8601 datetime
        status      – pending | partial | paid | cancelled
    """
    bill = db.query(Bill).filter(Bill.id == bill_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    old_snapshot = {
        "bill_type":   bill.bill_type,
        "description": bill.description,
        "amount":      _decimal_to_float(bill.amount),
        "due_date":    str(bill.due_date) if bill.due_date else None,
        "status":      bill.status,
    }

    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    # Apply simple field updates
    if "bill_type"   in changes: bill.bill_type   = changes["bill_type"]
    if "description" in changes: bill.description = changes["description"]
    if "bill_date"   in changes: bill.bill_date   = changes["bill_date"]
    if "due_date"    in changes: bill.due_date    = changes["due_date"]

    # Amount change requires reconciliation
    if "amount" in changes:
        new_amount = Decimal(str(changes["amount"]))
        already_paid = _decimal_to_float(bill.paid_amount)
        if changes["amount"] < already_paid:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reduce amount below already-paid amount ({already_paid}). "
                       "Delete excess payments first."
            )
        bill.amount = new_amount
        # Re-reconcile derived fields
        _reconcile_bill(bill)

    # Manual status override (e.g. marking cancelled)
    if "status" in changes:
        bill.status = changes["status"]
        if changes["status"] == "cancelled":
            bill.pending_amount = Decimal("0")

    write_audit(db, actor.id, "UPDATE", "bills", bill.id,
                old_data=old_snapshot, new_data=changes)
    db.commit()
    db.refresh(bill)

    return {
        "success": True,
        "message": "Bill updated successfully",
        "data": {
            "id":             bill.id,
            "user_id":        bill.user_id,
            "shop_id":        bill.shop_id,
            "bill_type":      bill.bill_type,
            "description":    bill.description,
            "amount":         _decimal_to_float(bill.amount),
            "paid_amount":    _decimal_to_float(bill.paid_amount),
            "pending_amount": _decimal_to_float(bill.pending_amount),
            "bill_date":      bill.bill_date,
            "due_date":       bill.due_date,
            "status":         bill.status,
        },
    }


@router.delete("/api/bill/{bill_id}", tags=["Bill"])
def delete_bill(
    bill_id: int,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Delete a bill and all its associated payments.
    This also re-reconciles the tenant's outstanding balance.
    Admin only.

    ⚠️  This is a hard-delete. Use with caution.
    """
    bill = db.query(Bill).filter(Bill.id == bill_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    old_snapshot = {
        "id":        bill.id, "user_id": bill.user_id, "shop_id": bill.shop_id,
        "bill_type": bill.bill_type, "amount": _decimal_to_float(bill.amount),
        "status":    bill.status,
    }

    # Delete linked payments first (cascade not guaranteed on all DB configs)
    db.query(Payment).filter(Payment.bill_id == bill_id).delete(synchronize_session=False)
    db.delete(bill)

    write_audit(db, actor.id, "DELETE", "bills", bill_id, old_data=old_snapshot)
    db.commit()
    return {"success": True, "message": f"Bill {bill_id} and its payments deleted successfully"}


@router.get("/api/bills", tags=["Bill"])
def list_bills_paginated(
    page:       int            = Query(1,  ge=1),
    limit:      int            = Query(20, ge=1, le=200),
    user_id:    Optional[int]  = None,
    shop_id:    Optional[int]  = None,
    complex_id: Optional[int]  = None,
    status:     Optional[str]  = Query(None, pattern="^(pending|partial|paid|cancelled)$"),
    bill_type:  Optional[str]  = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    search:     Optional[str]  = None,
    db:         Session        = Depends(get_db),
    _:          User           = Depends(require_admin),
):
    """
    Paginated, filterable list of all bills enriched with tenant name,
    shop number, and complex name.  Replaces the bare GET /api/bill list.
    Admin only.
    """
    q = (
        db.query(Bill, User, Shop)
        .join(User, User.id == Bill.user_id)
        .join(Shop, Shop.id == Bill.shop_id)
    )

    if user_id:    q = q.filter(Bill.user_id  == user_id)
    if shop_id:    q = q.filter(Bill.shop_id  == shop_id)
    if status:     q = q.filter(Bill.status   == status)
    if bill_type:  q = q.filter(Bill.bill_type.ilike(f"%{bill_type}%"))
    if start_date: q = q.filter(Bill.bill_date >= start_date)
    if end_date:   q = q.filter(Bill.bill_date <= end_date)
    if complex_id: q = q.filter(Shop.complex_id == complex_id)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            User.name.ilike(term)
            | User.mobile.ilike(term)
            | Shop.shop_number.ilike(term)
            | Bill.bill_type.ilike(term)
            | Bill.description.ilike(term)
        )

    total  = q.count()
    offset = (page - 1) * limit
    rows   = q.order_by(Bill.id.desc()).offset(offset).limit(limit).all()

    complexes = {c.id: c.name for c in db.query(Complex).all()}

    return {
        "success": True,
        "page":    page,
        "limit":   limit,
        "total":   total,
        "data": [
            {
                "id":             b.id,
                "bill_date":      b.bill_date,
                "due_date":       b.due_date,
                "bill_type":      b.bill_type,
                "description":    b.description,
                "amount":         _decimal_to_float(b.amount),
                "paid_amount":    _decimal_to_float(b.paid_amount),
                "pending_amount": _decimal_to_float(b.pending_amount),
                "status":         b.status,
                "user": {"id": u.id, "name": u.name, "mobile": u.mobile},
                "shop": {
                    "id":           s.id,
                    "shop_number":  s.shop_number,
                    "complex_id":   s.complex_id,
                    "complex_name": complexes.get(s.complex_id),
                },
            }
            for b, u, s in rows
        ],
    }
