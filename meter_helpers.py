"""
meter_helpers.py - shared helpers for the submeter-reading feature, used by
the meters CRUD router, the meter-readings admin workflow routes, the tenant
meter routes, and the tenant home bundle.

Extracted verbatim from app.py's SUBMETER READINGS section (step 9 of the
router/service split). Kept separate from domain_helpers.py because these
are meter-domain-specific, not general-purpose.
"""

from typing import List

from sqlalchemy.orm import Session
from fastapi import HTTPException

from create_tables import Bill, Meter, MeterReading, Shop, User, UserShop
from meter_service import MeterError
from domain_helpers import _decimal_to_float


def _meter_error(exc: MeterError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _tenant_shop_ids(db: Session, user_id: int) -> List[int]:
    """Shops currently assigned to this tenant - the basis of every ownership check."""
    return [us.shop_id for us in db.query(UserShop).filter(UserShop.user_id == user_id).all()]


def _reading_to_dict(db: Session, r: MeterReading, include_admin_fields: bool = False) -> dict:
    """
    Serialise a reading. Admin-only fields are omitted for tenants so the
    tenant response never leaks internal review notes.
    """
    meter = db.query(Meter).filter(Meter.id == r.meter_id).first()
    shop  = db.query(Shop).filter(Shop.id == r.shop_id).first()
    user  = db.query(User).filter(User.id == r.user_id).first()

    data = {
        "id": r.id,
        "meter_id": r.meter_id,
        "meter_number": meter.meter_number if meter else None,
        "meter_type": meter.meter_type if meter else None,
        "shop_id": r.shop_id,
        "shop_number": shop.shop_number if shop else None,
        "user_id": r.user_id,
        "user_name": user.name if user else None,
        "user_mobile": user.mobile if user else None,
        "previous_reading": _decimal_to_float(r.previous_reading),
        "customer_reading": _decimal_to_float(r.customer_reading),
        "customer_note": r.customer_note,
        "reading_date": r.reading_date,
        "status": r.status,
        "has_photo": bool(r.photo_path),
        "photo_url": f"/api/meter-readings/{r.id}/photo" if r.photo_path else None,
        "calculated_units": _decimal_to_float(r.calculated_units) if r.calculated_units is not None else None,
        "unit_price_applied": _decimal_to_float(r.unit_price_applied) if r.unit_price_applied is not None else None,
        "approved_reading": _decimal_to_float(r.approved_reading) if r.approved_reading is not None else None,
        "approved_at": r.approved_at,
        "rejection_reason": r.rejection_reason,
        "bill_id": r.bill_id,
        "created_at": r.created_at,
    }

    if r.bill_id:
        bill = db.query(Bill).filter(Bill.id == r.bill_id).first()
        if bill:
            data["bill"] = {
                "id": bill.id,
                "amount": _decimal_to_float(bill.amount),
                "paid_amount": _decimal_to_float(bill.paid_amount),
                "pending_amount": _decimal_to_float(bill.pending_amount),
                "status": bill.status,
                "due_date": bill.due_date,
                "description": bill.description,
            }

    if include_admin_fields:
        verifier = db.query(User).filter(User.id == r.admin_verified_by).first() if r.admin_verified_by else None
        data.update({
            "admin_verified_reading": _decimal_to_float(r.admin_verified_reading) if r.admin_verified_reading is not None else None,
            "admin_verified_by": r.admin_verified_by,
            "admin_verified_by_name": verifier.name if verifier else None,
            "admin_verified_at": r.admin_verified_at,
            "admin_note": r.admin_note,
            "override_reason": r.override_reason,
            "approved_by": r.approved_by,
            "rejected_by": r.rejected_by,
            "rejected_at": r.rejected_at,
            "photo_original_name": r.photo_original_name,
            "photo_size_bytes": r.photo_size_bytes,
        })

    return data
