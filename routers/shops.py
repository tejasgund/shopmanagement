"""
routers/shops.py - Shop CRUD + assign-complex (Admin only).

Extracted verbatim from app.py (step 15 of the router/service split).
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Complex, Shop, User
from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import _shop_owner_map, _shop_to_response
from schemas import AssignComplexRequest, ShopCreate, ShopResponse, ShopUpdate

router = APIRouter(tags=["Shop"])


@router.post("/api/shop", response_model=ShopResponse, status_code=201, tags=["Shop"])
def create_shop(
    body:  ShopCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Create a new shop. Admin only."""
    if body.complex_id:
        if not db.query(Complex).filter(Complex.id == body.complex_id).first():
            raise HTTPException(400, detail="Complex not found")

    obj = Shop(**body.model_dump())
    db.add(obj)
    db.flush()
    write_audit(db, actor.id, "CREATE", "shops", obj.id, new_data=body.model_dump())
    db.commit()
    db.refresh(obj)
    return _shop_to_response(obj, {})


@router.get("/api/shop", response_model=List[ShopResponse], tags=["Shop"])
def list_shops(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all shops, including current owner (if any). Admin only."""
    shops = db.query(Shop).order_by(Shop.id).all()
    owner_map = _shop_owner_map(db)
    return [_shop_to_response(s, owner_map) for s in shops]


@router.get("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def get_shop(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single shop, including current owner (if any). Admin only."""
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")
    owner_map = _shop_owner_map(db, [id])
    return _shop_to_response(obj, owner_map)


@router.put("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def update_shop(
    id:    int,
    body:  ShopUpdate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Update a shop. Admin only."""
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")

    old = {"shop_number": obj.shop_number, "status": obj.status, "complex_id": obj.complex_id}
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)

    write_audit(db, actor.id, "UPDATE", "shops", id, old_data=old, new_data=body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(obj)
    owner_map = _shop_owner_map(db, [id])
    return _shop_to_response(obj, owner_map)


@router.delete("/api/shop/{id}", tags=["Shop"])
def delete_shop(
    id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin)
):
    shop = db.query(Shop).filter(Shop.id == id).first()

    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    # Prevent deleting a shop that has bills
    if shop.bills:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete shop. {len(shop.bills)} bill(s) exist for this shop."
        )

    db.delete(shop)
    db.commit()

    return {
        "success": True,
        "message": f"Shop {id} deleted successfully"
    }


@router.post("/api/shop/{shop_id}/assign-complex", tags=["Shop"])
def assign_complex_to_shop(
    shop_id: int,
    body:    AssignComplexRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Assign a shop to a complex. One shop can belong to only one complex.
    Admin only.
    """
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")

    complex_obj = db.query(Complex).filter(Complex.id == body.complex_id).first()
    if not complex_obj:
        raise HTTPException(404, detail="Complex not found")

    old_complex_id  = shop.complex_id
    shop.complex_id = body.complex_id

    write_audit(
        db, actor.id, "UPDATE", "shops", shop_id,
        old_data={"complex_id": old_complex_id},
        new_data={"complex_id": body.complex_id},
    )
    db.commit()
    return {"success": True, "message": f"Shop {shop_id} assigned to complex {body.complex_id}"}
