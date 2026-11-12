"""
routers/meter_tariffs.py - POST/GET /api/meter-tariffs, DELETE /api/meter-tariffs/{id}
(Admin only).

Extracted verbatim from app.py (step 8 of the router/service split).
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.config import APP_TIMEZONE
from core.database import get_db
from core.security import require_admin
from models.schema import MeterReading, MeterTariff, User
from schemas.api import TariffCreate
from services import meter as meter_service
from services.audit import write_audit
from helpers.domain import _decimal_to_float


router = APIRouter(tags=["Meter"])


@router.post("/api/meter-tariffs", tags=["Meter"], status_code=201)
def create_tariff(
    payload: TariffCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Add a unit price effective from a date. Rates are never edited in place -
    a new row is added so that already-issued bills keep the rate they used.
    """
    tariff = MeterTariff(
        meter_type     = (payload.meter_type or "electricity").strip().lower(),
        unit_price     = Decimal(str(payload.unit_price)),
        fixed_charge   = Decimal(str(payload.fixed_charge or 0)),
        tax_percent    = Decimal(str(payload.tax_percent or 0)),
        effective_from = payload.effective_from,
        notes          = payload.notes,
        created_by     = actor.id,
    )
    db.add(tariff)
    db.flush()
    write_audit(db, actor.id, "CREATE", "meter_tariffs", tariff.id, new_data={
        "meter_type": tariff.meter_type, "unit_price": float(tariff.unit_price),
        "effective_from": tariff.effective_from.isoformat(),
    })
    db.commit()
    db.refresh(tariff)
    return _tariff_to_dict(tariff)


def _tariff_to_dict(t: MeterTariff) -> dict:
    return {
        "id": t.id,
        "meter_type": t.meter_type,
        "unit_price": _decimal_to_float(t.unit_price),
        "fixed_charge": _decimal_to_float(t.fixed_charge),
        "tax_percent": _decimal_to_float(t.tax_percent),
        "effective_from": t.effective_from,
        "notes": t.notes,
        "created_at": t.created_at,
    }


@router.get("/api/meter-tariffs", tags=["Meter"])
def list_tariffs(
    meter_type: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Full rate history, newest first. Admin only."""
    q = db.query(MeterTariff)
    if meter_type:
        q = q.filter(MeterTariff.meter_type == meter_type.strip().lower())
    rows = q.order_by(MeterTariff.effective_from.desc(), MeterTariff.id.desc()).all()

    now = datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)
    current = meter_service.applicable_tariff(db, meter_type or "electricity", now)
    return {
        "current_tariff_id": current.id if current else None,
        "tariffs": [_tariff_to_dict(t) for t in rows],
    }


@router.delete("/api/meter-tariffs/{id}", tags=["Meter"])
def delete_tariff(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """
    Delete a rate. Refused if a bill was already issued using it, since that
    would break the audit trail behind an existing bill.
    """
    tariff = db.query(MeterTariff).filter(MeterTariff.id == id).first()
    if not tariff:
        raise HTTPException(404, detail="Tariff not found")

    used = db.query(MeterReading).filter(MeterReading.tariff_id == tariff.id).first()
    if used:
        raise HTTPException(
            400,
            detail="This rate has already been used to bill a reading and cannot be "
                   "deleted. Add a newer rate instead.",
        )

    write_audit(db, actor.id, "DELETE", "meter_tariffs", tariff.id, old_data=_tariff_to_dict(tariff))
    db.delete(tariff)
    db.commit()
    return {"success": True, "message": "Tariff deleted"}
