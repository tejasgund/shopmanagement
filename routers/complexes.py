"""
routers/complexes.py - Complex CRUD + summary endpoints (Admin only).

Extracted verbatim from app.py (step 14 of the router/service split).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from db_config import get_db
from create_tables import Bill, Complex, Shop, User, UserShop
from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import (
    _decimal_to_float, _shop_owner_map, _deposit_paid_for_shop, _pending_rent_for_user,
)
from schemas import ComplexCreate, ComplexResponse, ComplexUpdate

router = APIRouter(tags=["Complex"])


@router.post("/api/complex", response_model=ComplexResponse, status_code=201, tags=["Complex"])
def create_complex(
    body: ComplexCreate,
    db:   Session = Depends(get_db),
    actor: User   = Depends(require_admin),
):
    """Create a new complex. Admin only."""
    obj = Complex(**body.model_dump())
    db.add(obj)
    db.flush()
    write_audit(db, actor.id, "CREATE", "complexes", obj.id, new_data=body.model_dump())
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/api/complex", response_model=List[ComplexResponse], tags=["Complex"])
def list_complexes(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all complexes. Admin only."""
    return db.query(Complex).order_by(Complex.id).all()


# NOTE: this concrete path MUST be registered before "/api/complex/{id}" so
# FastAPI doesn't try to parse "summary" as an integer id.
@router.get("/api/complex/summary", tags=["Complex"])
def all_complexes_summary(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Summary statistics for ALL complexes. Used on the main Admin Dashboard. Admin only."""
    complexes = db.query(Complex).order_by(Complex.id).all()

    # Two bulk queries instead of two PER complex (this used to be N+1 - one
    # extra shops query and one extra bills query for every single complex).
    # Same numbers as before, computed once and grouped in Python instead.
    shops_by_complex = {}
    for s in db.query(Shop).all():
        shops_by_complex.setdefault(s.complex_id, []).append(s)

    pending_rent_by_complex = {}
    for b, complex_id in (
        db.query(Bill, Shop.complex_id)
        .join(Shop, Shop.id == Bill.shop_id)
        .filter(Bill.bill_type == "Rent", Bill.status != "paid")
        .all()
    ):
        pending_rent_by_complex.setdefault(complex_id, []).append(b)

    results = []
    for c in complexes:
        shops = shops_by_complex.get(c.id, [])
        total_shops = len(shops)
        occupied = [s for s in shops if s.status == "occupied"]
        available_count = sum(1 for s in shops if s.status == "available")
        total_monthly_rent = sum(_decimal_to_float(s.shop_rent) for s in occupied)
        total_pending_rent = pending_rent_by_complex.get(c.id, [])
        results.append({
            "complex_id": c.id,
            "complex_name": c.name,
            "total_shops": total_shops,
            "occupied_shops": len(occupied),
            "available_shops": available_count,
            "total_monthly_rent": round(total_monthly_rent, 2),
            "total_pending_rent": round(sum(_decimal_to_float(b.pending_amount) for b in total_pending_rent), 2),
        })
    return results


@router.get("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def get_complex(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")
    return obj


@router.get("/api/complex/{id}/summary", tags=["Complex"])
def complex_summary(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Live statistics for a single complex. Used in the Complex Dashboard cards. Admin only."""
    c = db.query(Complex).filter(Complex.id == id).first()
    if not c:
        raise HTTPException(404, detail="Complex not found")

    shops = db.query(Shop).filter(Shop.complex_id == id).all()
    shop_ids = [s.id for s in shops]
    occupied = [s for s in shops if s.status == "occupied"]
    available_count = sum(1 for s in shops if s.status == "available")

    owner_map = _shop_owner_map(db, shop_ids) if shop_ids else {}

    total_monthly_rent = 0.0
    total_deposit_required = 0.0
    tenants_by_user = {}

    for s in shops:
        us = db.query(UserShop).filter(UserShop.shop_id == s.id).order_by(UserShop.assigned_at.desc()).first()
        deposit_required = _decimal_to_float(s.shop_deposit)
        total_deposit_required += deposit_required if s.status == "occupied" else 0.0
        if s.status != "occupied" or not us:
            continue
        rent = _decimal_to_float(s.shop_rent)  # <-- DIRECT USE
        total_monthly_rent += rent

        owner = owner_map.get(s.id)
        if not owner:
            continue
        entry = tenants_by_user.setdefault(owner.id, {
            "user_id": owner.id, "user_name": owner.name, "mobile": owner.mobile,
            "shops": [], "monthly_rent": 0.0, "pending_rent": 0.0,
            "deposit_required": 0.0, "deposit_paid": 0.0,
        })
        entry["shops"].append(s.shop_number)
        entry["monthly_rent"] += rent
        entry["deposit_required"] += deposit_required
        entry["deposit_paid"] += _deposit_paid_for_shop(db, owner.id, s.id)
        entry["pending_rent"] = _pending_rent_for_user(db, owner.id)  # per-user total, same each time it's set

    total_pending_rent = sum(
        _decimal_to_float(b.pending_amount)
        for b in db.query(Bill).join(Shop, Shop.id == Bill.shop_id)
        .filter(Shop.complex_id == id, Bill.bill_type == "Rent", Bill.status != "paid").all()
    )

    tenants = []
    for entry in tenants_by_user.values():
        entry["deposit_remaining"] = round(entry["deposit_required"] - entry["deposit_paid"], 2)
        entry["monthly_rent"] = round(entry["monthly_rent"], 2)
        entry["deposit_required"] = round(entry["deposit_required"], 2)
        entry["deposit_paid"] = round(entry["deposit_paid"], 2)
        entry["pending_rent"] = round(entry["pending_rent"], 2)
        tenants.append(entry)

    total_deposit_collected = sum(t["deposit_paid"] for t in tenants)

    return {
        "complex_id": c.id,
        "complex_name": c.name,
        "address": c.address,
        "stats": {
            "total_shops": len(shops),
            "occupied_shops": len(occupied),
            "available_shops": available_count,
            "total_monthly_rent": round(total_monthly_rent, 2),
            "total_pending_rent": round(total_pending_rent, 2),
            "total_deposit_required": round(total_deposit_required, 2),
            "total_deposit_collected": round(total_deposit_collected, 2),
            "total_deposit_remaining": round(total_deposit_required - total_deposit_collected, 2),
        },
        "tenants": tenants,
    }


@router.put("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def update_complex(
    id:   int,
    body: ComplexUpdate,
    db:   Session = Depends(get_db),
    actor:User    = Depends(require_admin),
):
    """Update a complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")

    old = {"name": obj.name, "address": obj.address, "description": obj.description}
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)

    write_audit(db, actor.id, "UPDATE", "complexes", id, old_data=old, new_data=body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/api/complex/{id}", tags=["Complex"])
def delete_complex(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Delete a complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")

    write_audit(db, actor.id, "DELETE", "complexes", id, old_data={"name": obj.name})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "Complex deleted"}
