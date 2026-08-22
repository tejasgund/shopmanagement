"""
routers/search.py - GET /api/search (Admin only).

Extracted verbatim from app.py (step 6a of the router/service split).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Complex, Shop, User, UserShop
from auth_service import require_admin
from domain_helpers import _decimal_to_float, _shop_owner_map

router = APIRouter(tags=["Search"])


@router.get("/api/search", tags=["Search"])
def global_search(
    q:  str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _:  User    = Depends(require_admin),
):
    """Case-insensitive search across users, shops, and complexes. Admin only."""
    term = f"%{q.strip().lower()}%"

    users = db.query(User).filter(
        (User.name.ilike(term)) | (User.mobile.ilike(term))
    ).all()
    user_shops_map = {}
    if users:
        rows = (
            db.query(UserShop, Shop)
            .join(Shop, Shop.id == UserShop.shop_id)
            .filter(UserShop.user_id.in_([u.id for u in users]))
            .all()
        )
        for us, s in rows:
            user_shops_map.setdefault(us.user_id, []).append(s.shop_number)

    shops = db.query(Shop).filter(Shop.shop_number.ilike(term)).all()
    shop_ids = [s.id for s in shops]
    owner_map = _shop_owner_map(db, shop_ids) if shop_ids else {}
    complexes_by_id = {c.id: c.name for c in db.query(Complex).all()}

    complexes = db.query(Complex).filter(
        (Complex.name.ilike(term)) | (Complex.address.ilike(term))
    ).all()

    return {
        "users": [
            {
                "id": u.id, "name": u.name, "mobile": u.mobile, "email": u.email,
                "role": u.role, "is_active": u.is_active,
                "shops": user_shops_map.get(u.id, []),
            }
            for u in users
        ],
        "shops": [
            {
                "id": s.id, "shop_number": s.shop_number, "complex_id": s.complex_id,
                "complex_name": complexes_by_id.get(s.complex_id), "status": s.status,
                "shop_rent": _decimal_to_float(s.shop_rent), "shop_deposit": _decimal_to_float(s.shop_deposit),
                "assigned_to": owner_map.get(s.id).model_dump() if owner_map.get(s.id) else None,
            }
            for s in shops
        ],
        "complexes": [
            {"id": c.id, "name": c.name, "address": c.address}
            for c in complexes
        ],
    }
