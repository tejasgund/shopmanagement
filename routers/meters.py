"""
routers/meters.py - Meter CRUD + assign/unassign/history (Admin only).

Extracted verbatim from app.py (step 10 of the router/service split).
"""

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, Meter, MeterReading, Shop, User, UserShop
from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import _decimal_to_float
from meter_helpers import _reading_to_dict
from schemas import AssignMeterShopRequest, MeterCreate, MeterUpdate
import meter_service

router = APIRouter(tags=["Meter"])


@router.post("/api/meters", tags=["Meter"], status_code=201)
def create_meter(
    payload: MeterCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Register a submeter. shop_id is optional - a meter with no shop sits in the
    "not assigned" list until an admin assigns it. Admin only.
    """
    if payload.shop_id is not None:
        shop = db.query(Shop).filter(Shop.id == payload.shop_id).first()
        if not shop:
            raise HTTPException(404, detail="Shop not found")
        clash = (
            db.query(Meter)
            .filter(Meter.shop_id == payload.shop_id, Meter.meter_number == payload.meter_number)
            .first()
        )
        if clash:
            raise HTTPException(409, detail=f"Meter {payload.meter_number} already exists on this shop")
    else:
        # Unassigned meters aren't covered by the (shop_id, meter_number) unique
        # index because SQL treats NULLs as distinct - check by hand.
        clash = (
            db.query(Meter)
            .filter(Meter.shop_id.is_(None), Meter.meter_number == payload.meter_number)
            .first()
        )
        if clash:
            raise HTTPException(409, detail=f"An unassigned meter numbered {payload.meter_number} already exists")

    meter = Meter(
        shop_id           = payload.shop_id,
        meter_number      = payload.meter_number.strip(),
        meter_type        = (payload.meter_type or "electricity").strip().lower(),
        initial_reading   = Decimal(str(payload.initial_reading or 0)),
        installation_date = payload.installation_date,
        notes             = payload.notes,
        is_active         = payload.is_active if payload.is_active is not None else True,
    )
    db.add(meter)
    db.flush()
    write_audit(db, actor.id, "CREATE", "meters", meter.id, new_data={
        "shop_id": meter.shop_id, "meter_number": meter.meter_number,
        "initial_reading": float(meter.initial_reading),
    })
    db.commit()
    db.refresh(meter)
    return _meter_to_dict(db, meter)


def _meter_to_dict(db: Session, m: Meter) -> dict:
    shop = db.query(Shop).filter(Shop.id == m.shop_id).first() if m.shop_id else None
    complex_name = None
    if shop and shop.complex_id:
        cx = db.query(Complex).filter(Complex.id == shop.complex_id).first()
        complex_name = cx.name if cx else None
    owner = None
    if shop:
        us = db.query(UserShop).filter(UserShop.shop_id == shop.id).first()
        if us:
            u = db.query(User).filter(User.id == us.user_id).first()
            if u:
                owner = {"id": u.id, "name": u.name, "mobile": u.mobile}

    last = meter_service.latest_approved_reading(db, m.id)
    pending = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == m.id, MeterReading.status == "pending")
        .first()
    )
    reading_count = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == m.id, MeterReading.status == "approved")
        .count()
    )

    return {
        "id": m.id,
        "shop_id": m.shop_id,
        "shop_number": shop.shop_number if shop else None,
        "complex_id": shop.complex_id if shop else None,
        "complex_name": complex_name,
        "is_assigned": m.shop_id is not None,
        "assigned_to": owner,
        "meter_number": m.meter_number,
        "meter_type": m.meter_type,
        "initial_reading": _decimal_to_float(m.initial_reading),
        "installation_date": m.installation_date,
        "notes": m.notes,
        "is_active": m.is_active,
        # "Current reading" = the latest reading an admin has approved. This is
        # what the meter is billed up to, and what the next reading counts from.
        "current_reading": float(meter_service.previous_reading_value(db, m)),
        "last_approved_reading": _decimal_to_float(last.approved_reading) if last else None,
        "last_reading_date": last.reading_date if last else None,
        "last_updated": last.approved_at if last else None,
        "reading_count": reading_count,
        "has_pending_reading": pending is not None,
        "pending_reading_id": pending.id if pending else None,
        "created_at": m.created_at,
        # Kept for backward compatibility with the first version of this API.
        "current_previous_reading": float(meter_service.previous_reading_value(db, m)),
    }


@router.get("/api/meters", tags=["Meter"])
def list_meters(
    shop_id:    Optional[int]  = None,
    complex_id: Optional[int]  = None,
    is_active:  Optional[bool] = None,
    assigned:   Optional[bool] = Query(None, description="true = only meters on a shop, false = only unassigned"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """All registered submeters, optionally filtered. Admin only."""
    q = db.query(Meter)
    if shop_id is not None:
        q = q.filter(Meter.shop_id == shop_id)
    if is_active is not None:
        q = q.filter(Meter.is_active == is_active)
    if assigned is True:
        q = q.filter(Meter.shop_id.isnot(None))
    elif assigned is False:
        q = q.filter(Meter.shop_id.is_(None))
    if complex_id is not None:
        q = q.join(Shop, Shop.id == Meter.shop_id).filter(Shop.complex_id == complex_id)
    return [_meter_to_dict(db, m) for m in q.order_by(Meter.id.desc()).all()]


@router.post("/api/meters/{id}/assign-shop", tags=["Meter"])
def assign_meter_to_shop(
    id: int,
    payload: AssignMeterShopRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Put a meter on a shop (or move it to a different one).

    Moving a meter that already has approved readings is refused: its history
    is tied to the old shop's bills, and silently moving it would make those
    bills unexplainable. Register a separate meter on the new shop instead.
    """
    meter = db.query(Meter).filter(Meter.id == id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")

    shop = db.query(Shop).filter(Shop.id == payload.shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")

    if meter.shop_id == shop.id:
        raise HTTPException(400, detail=f"This meter is already on shop {shop.shop_number}")

    if meter.shop_id is not None:
        has_history = (
            db.query(MeterReading)
            .filter(MeterReading.meter_id == meter.id, MeterReading.status == "approved")
            .first()
        )
        if has_history:
            raise HTTPException(
                400,
                detail="This meter already has approved readings billed to its current shop, "
                       "so it can't be moved. Add a new meter on the other shop instead.",
            )

    clash = (
        db.query(Meter)
        .filter(Meter.shop_id == shop.id, Meter.meter_number == meter.meter_number, Meter.id != meter.id)
        .first()
    )
    if clash:
        raise HTTPException(409, detail=f"Shop {shop.shop_number} already has a meter numbered {meter.meter_number}")

    old_shop_id = meter.shop_id
    meter.shop_id = shop.id
    write_audit(db, actor.id, "ASSIGN", "meters", meter.id,
                old_data={"shop_id": old_shop_id},
                new_data={"shop_id": shop.id, "shop_number": shop.shop_number})
    db.commit()
    db.refresh(meter)
    return {
        "success": True,
        "message": f"Meter {meter.meter_number} assigned to shop {shop.shop_number}",
        "meter": _meter_to_dict(db, meter),
    }


@router.post("/api/meters/{id}/unassign", tags=["Meter"])
def unassign_meter(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """
    Take a meter off its shop and return it to the unassigned list.

    Its past readings and the bills they produced are untouched - those record
    the shop they were billed to. The tenant simply stops seeing this meter.
    Refused while a reading is awaiting review, so nothing is left orphaned.
    """
    meter = db.query(Meter).filter(Meter.id == id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")
    if meter.shop_id is None:
        raise HTTPException(400, detail="This meter is not assigned to a shop")

    pending = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == meter.id, MeterReading.status == "pending")
        .first()
    )
    if pending:
        raise HTTPException(
            400,
            detail="A reading for this meter is waiting for review. Approve or reject it "
                   "before removing the meter from the shop.",
        )

    old_shop_id = meter.shop_id
    meter.shop_id = None
    write_audit(db, actor.id, "UNASSIGN", "meters", meter.id,
                old_data={"shop_id": old_shop_id}, new_data={"shop_id": None})
    db.commit()
    db.refresh(meter)
    return {
        "success": True,
        "message": f"Meter {meter.meter_number} is now unassigned",
        "meter": _meter_to_dict(db, meter),
    }


@router.get("/api/meters/{id}/history", tags=["Meter"])
def meter_history(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    Everything that has ever happened on one meter: every submission with its
    photo, what was billed, and running totals. Powers the meter detail screen.
    """
    meter = db.query(Meter).filter(Meter.id == id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")

    rows = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == meter.id)
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .all()
    )

    entries, approved = [], []
    for r in rows:
        entry = _reading_to_dict(db, r, include_admin_fields=True)
        entry["units"] = _decimal_to_float(r.calculated_units) if r.calculated_units is not None else None
        entry["amount"] = None
        if r.bill_id:
            bill = db.query(Bill).filter(Bill.id == r.bill_id).first()
            if bill:
                entry["amount"] = _decimal_to_float(bill.amount)
        entries.append(entry)
        if r.status == "approved":
            approved.append(r)

    total_units = sum(_decimal_to_float(r.calculated_units) for r in approved if r.calculated_units)
    total_billed = 0.0
    for r in approved:
        if r.bill_id:
            bill = db.query(Bill).filter(Bill.id == r.bill_id).first()
            if bill:
                total_billed += _decimal_to_float(bill.amount)

    average_units = round(total_units / len(approved), 2) if approved else None

    return {
        "meter": _meter_to_dict(db, meter),
        "summary": {
            "total_readings": len(rows),
            "approved_count": len(approved),
            "pending_count": sum(1 for r in rows if r.status == "pending"),
            "rejected_count": sum(1 for r in rows if r.status == "rejected"),
            "total_units": round(total_units, 2),
            "total_billed": round(total_billed, 2),
            "average_units_per_reading": average_units,
            "first_reading_date": rows[-1].reading_date if rows else None,
            "latest_reading_date": rows[0].reading_date if rows else None,
        },
        "readings": entries,
    }


@router.get("/api/meters/{id}", tags=["Meter"])
def get_meter(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    meter = db.query(Meter).filter(Meter.id == id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")
    return _meter_to_dict(db, meter)


@router.put("/api/meters/{id}", tags=["Meter"])
def update_meter(
    id: int,
    payload: MeterUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    meter = db.query(Meter).filter(Meter.id == id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")

    old = {
        "meter_number": meter.meter_number, "meter_type": meter.meter_type,
        "initial_reading": float(meter.initial_reading), "is_active": meter.is_active,
    }

    if payload.meter_number is not None:
        clash = (
            db.query(Meter)
            .filter(Meter.shop_id == meter.shop_id,
                    Meter.meter_number == payload.meter_number,
                    Meter.id != meter.id)
            .first()
        )
        if clash:
            raise HTTPException(409, detail=f"Meter {payload.meter_number} already exists on this shop")
        meter.meter_number = payload.meter_number.strip()

    if payload.meter_type is not None:
        meter.meter_type = payload.meter_type.strip().lower()
    if payload.initial_reading is not None:
        # Changing this after readings exist would silently rewrite the basis of
        # the first bill, so only allow it while the meter has no approved history.
        has_history = (
            db.query(MeterReading)
            .filter(MeterReading.meter_id == meter.id, MeterReading.status == "approved")
            .first()
        )
        if has_history:
            raise HTTPException(
                400,
                detail="Initial reading cannot be changed once this meter has approved readings.",
            )
        meter.initial_reading = Decimal(str(payload.initial_reading))
    if payload.installation_date is not None:
        meter.installation_date = payload.installation_date
    if payload.notes is not None:
        meter.notes = payload.notes
    if payload.is_active is not None:
        meter.is_active = payload.is_active

    write_audit(db, actor.id, "UPDATE", "meters", meter.id, old_data=old, new_data={
        "meter_number": meter.meter_number, "meter_type": meter.meter_type,
        "initial_reading": float(meter.initial_reading), "is_active": meter.is_active,
    })
    db.commit()
    db.refresh(meter)
    return _meter_to_dict(db, meter)


@router.delete("/api/meters/{id}", tags=["Meter"])
def delete_meter(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """
    Remove a meter. Refused once it has approved readings - those are billing
    evidence. Deactivate it instead (is_active = false).
    """
    meter = db.query(Meter).filter(Meter.id == id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")

    approved = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == meter.id, MeterReading.status == "approved")
        .first()
    )
    if approved:
        raise HTTPException(
            400,
            detail="This meter has approved readings and cannot be deleted. "
                   "Mark it inactive instead so its history is preserved.",
        )

    write_audit(db, actor.id, "DELETE", "meters", meter.id, old_data={
        "shop_id": meter.shop_id, "meter_number": meter.meter_number,
    })
    db.delete(meter)
    db.commit()
    return {"success": True, "message": f"Meter {meter.meter_number} deleted"}
