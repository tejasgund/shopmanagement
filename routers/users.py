"""
routers/users.py - User CRUD, password reset, financial summary, and
shop assignment/detachment (Admin only).

Extracted verbatim from app.py (step 16 of the router/service split).
"""

from typing import List

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.database import get_db
from core.security import require_admin
from models.schema import Shop, User, UserShop
from schemas.api import (
    AssignShopsRequest, DetachShopsRequest, ResetPasswordRequest,
    UpdateAgreementRequest, UserCreate, UserResponse, UserUpdate,
)
from services.audit import write_audit
from helpers.domain import _shop_owner_map, _build_user_financial_summary

router = APIRouter(tags=["User"])


@router.post("/api/user", response_model=UserResponse, status_code=201, tags=["User"])
def create_user(
    body:  UserCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Create a new user (admin or tenant). Admin only."""
    if db.query(User).filter(User.mobile == body.mobile).first():
        raise HTTPException(400, detail="Mobile number already registered")

    if body.email and db.query(User).filter(User.email == body.email).first():
        raise HTTPException(400, detail="Email already registered")

    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()

    obj = User(
        name                   = body.name,
        mobile                 = body.mobile,
        email                  = body.email,
        password_hash          = password_hash,
        role                   = body.role or "tenant",
        rent_bill_date         = body.rent_bill_date,
        auto_rent_bill_enabled = body.auto_rent_bill_enabled,
        is_active              = True,
    )
    db.add(obj)
    db.flush()

    audit_data = body.model_dump(exclude={"password"})
    write_audit(db, actor.id, "CREATE", "users", obj.id, new_data=audit_data)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/api/user", response_model=List[UserResponse], tags=["User"])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all users. Admin only."""
    return db.query(User).order_by(User.id).all()


@router.get("/api/user/{id}", response_model=UserResponse, tags=["User"])
def get_user(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single user. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")
    return obj


@router.put("/api/user/{id}", response_model=UserResponse, tags=["User"])
def update_user(
    id:    int,
    body:  UserUpdate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Update a user. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")

    old = {"name": obj.name, "mobile": obj.mobile, "role": obj.role, "is_active": obj.is_active}
    was_active = obj.is_active

    update_data = body.model_dump(exclude_unset=True)
    if "password" in update_data:
        obj.password_hash = bcrypt.hashpw(update_data.pop("password").encode(), bcrypt.gensalt()).decode()

    for field, value in update_data.items():
        setattr(obj, field, value)

    released_shops = []
    # Deactivation auto-releases all shops owned by this user back to "available".
    # Bills and payments already linked to this user are left untouched for records.
    if was_active and obj.is_active is False:
        owned_rows = db.query(UserShop).filter(UserShop.user_id == id).all()
        for row in owned_rows:
            shop = db.query(Shop).filter(Shop.id == row.shop_id).first()
            if shop:
                shop.status = "available"
            released_shops.append(row.shop_id)
            db.delete(row)

    write_audit(db, actor.id, "UPDATE", "users", id, old_data=old,
                new_data={**{k: v for k, v in update_data.items() if k != "password"},
                          "released_shops": released_shops} if released_shops
                          else {k: v for k, v in update_data.items() if k != "password"})
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/api/user/{id}", tags=["User"])
def delete_user(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Delete a user. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")
    if obj.id == actor.id:
        raise HTTPException(400, detail="Cannot delete your own account")

    write_audit(db, actor.id, "DELETE", "users", id, old_data={"mobile": obj.mobile, "role": obj.role})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "User deleted"}


@router.put("/api/user/{id}/reset-password", tags=["User"])
def reset_password(
    id:    int,
    body:  ResetPasswordRequest,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Admin resets a user's password without knowing the old one. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")

    obj.password_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    write_audit(db, actor.id, "UPDATE", "users", id, new_data={"action": "password_reset"})
    db.commit()
    return {"message": "Password updated successfully"}


@router.get("/api/user/{id}/financial-summary", tags=["User"])
def user_financial_summary(
    id: int,
    db: Session = Depends(get_db),
    _:  User    = Depends(require_admin),
):
    """Full financial picture for a specific user/tenant. Admin only."""
    user = db.query(User).filter(User.id == id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    return _build_user_financial_summary(db, user)


@router.post("/api/user/{user_id}/assign-shops", tags=["User"])
def assign_shops_to_user(
    user_id: int,
    body:    AssignShopsRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Assign one or more shops to a user.

    A shop can have only ONE current owner at a time. If a requested shop is
    already owned by a different active user:
      - force=false (default): the whole request is rejected with 409,
        listing which shops are already taken and by whom, so the admin can
        confirm before reassigning.
      - force=true: the shop is detached from its previous owner first, then
        assigned to the new user (full reassignment).

    On success, every assigned shop's status is set to "occupied".
    Admin only.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    if not user.is_active:
        raise HTTPException(400, detail="Cannot assign shops to a deactivated user")

    # Validate shops exist first
    shops = {}
    for shop_id in body.shop_ids:
        shop = db.query(Shop).filter(Shop.id == shop_id).first()
        if not shop:
            raise HTTPException(400, detail=f"Shop {shop_id} not found")
        shops[shop_id] = shop

    owner_map = _shop_owner_map(db, body.shop_ids)

    # Find conflicts: shops already owned by a DIFFERENT user
    conflicts = {
        sid: owner for sid, owner in owner_map.items()
        if sid in shops and owner.id != user_id
    }

    if conflicts and not body.force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Some shops are already assigned to another tenant. "
                           "Resend with force=true to reassign.",
                "conflicts": [
                    {"shop_id": sid, "shop_number": shops[sid].shop_number,
                     "current_owner_id": owner.id, "current_owner_name": owner.name}
                    for sid, owner in conflicts.items()
                ],
            },
        )

    assigned = []
    reassigned_from = []
    for shop_id, shop in shops.items():
        # If force=true and shop has a different owner, detach the old owner first
        if shop_id in conflicts:
            old_row = db.query(UserShop).filter(UserShop.shop_id == shop_id).first()
            if old_row:
                db.delete(old_row)
                reassigned_from.append({"shop_id": shop_id, "from_user_id": conflicts[shop_id].id})

        existing_row = db.query(UserShop).filter(
            UserShop.user_id == user_id, UserShop.shop_id == shop_id
        ).first()
        if not existing_row:
            # No rent stored – the assignment is now purely a relationship
            db.add(UserShop(
                user_id=user_id,
                shop_id=shop_id,
                agreement_start_date=body.agreement_start_date,
                agreement_end_date=body.agreement_end_date,
            ))
            assigned.append(shop_id)
        else:
            # Already assigned to this same user — still update agreement dates
            # if the admin provided new ones via this same modal.
            if body.agreement_start_date is not None:
                existing_row.agreement_start_date = body.agreement_start_date
            if body.agreement_end_date is not None:
                existing_row.agreement_end_date = body.agreement_end_date

        shop.status = "occupied"

    write_audit(db, actor.id, "ASSIGN_SHOPS", "user_shops", user_id,
                old_data={"reassigned_from": reassigned_from} if reassigned_from else None,
                new_data={"user_id": user_id, "shop_ids": assigned})
    db.commit()
    return {
        "success": True,
        "message": f"Assigned shops {assigned} to user {user_id}",
        "reassigned_from": reassigned_from,
    }


@router.put("/api/user/{user_id}/shop/{shop_id}/agreement", tags=["User"])
def update_agreement_dates(
        user_id: int,
        shop_id: int,
        body: UpdateAgreementRequest,
        db: Session = Depends(get_db),
        actor: User = Depends(require_admin),
):
    """Update (or set) the agreement start/end dates for an existing tenant-shop assignment. Admin only."""
    row = db.query(UserShop).filter(
        UserShop.user_id == user_id, UserShop.shop_id == shop_id
    ).first()
    if not row:
        raise HTTPException(404, detail="This shop is not assigned to this user")

    old_data = {
        "agreement_start_date": row.agreement_start_date.isoformat() if row.agreement_start_date else None,
        "agreement_end_date": row.agreement_end_date.isoformat() if row.agreement_end_date else None,
    }
    row.agreement_start_date = body.agreement_start_date
    row.agreement_end_date = body.agreement_end_date

    write_audit(db, actor.id, "UPDATE_AGREEMENT", "user_shops", user_id,
                old_data=old_data,
                new_data={"agreement_start_date": str(body.agreement_start_date),
                          "agreement_end_date": str(body.agreement_end_date)})
    db.commit()
    return {"success": True, "message": "Agreement dates updated"}


@router.post("/api/user/{user_id}/detach-shops", tags=["User"])
def detach_shops_from_user(
    user_id: int,
    body:    DetachShopsRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """Detach one or more shops from a user and mark them available. Admin only."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")

    detached = []
    for shop_id in body.shop_ids:
        row = db.query(UserShop).filter(
            UserShop.user_id == user_id, UserShop.shop_id == shop_id
        ).first()
        if row:
            db.delete(row)
            detached.append(shop_id)
            shop = db.query(Shop).filter(Shop.id == shop_id).first()
            if shop:
                shop.status = "available"

    write_audit(db, actor.id, "DETACH_SHOPS", "user_shops", user_id,
                old_data={"user_id": user_id, "shop_ids": detached})
    db.commit()
    return {"success": True, "message": f"Detached shops {detached} from user {user_id}"}
