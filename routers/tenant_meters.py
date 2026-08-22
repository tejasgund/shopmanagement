"""
routers/tenant_meters.py - GET /api/tenant/meters, POST /api/tenant/meter-readings,
GET /api/tenant/meter-readings(/{id}), GET /api/tenant/meters/{meter_id}/readings.

Extracted verbatim from app.py (step 11 of the router/service split).
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Meter, MeterReading, Shop, User
from auth_service import require_tenant
from audit_service import write_audit
from meter_helpers import _tenant_shop_ids, _reading_to_dict
from app_config import APP_TIMEZONE
from log import get_logger
import meter_service
import photo_storage
import settings_service

logger = get_logger("app")

router = APIRouter(tags=["Tenant"])


@router.get("/api/tenant/meters", tags=["Tenant"])
def tenant_meters(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """
    Meters on the shops assigned to the signed-in tenant, each with the number
    their next reading must be above and whether a submission is already
    awaiting review.
    """
    shop_ids = _tenant_shop_ids(db, current_user.id)
    if not shop_ids:
        return []

    meters = (
        db.query(Meter)
        .filter(Meter.shop_id.in_(shop_ids), Meter.is_active == True)
        .order_by(Meter.id)
        .all()
    )

    result = []
    for m in meters:
        shop = db.query(Shop).filter(Shop.id == m.shop_id).first()
        pending = (
            db.query(MeterReading)
            .filter(
                MeterReading.meter_id == m.id,
                MeterReading.user_id == current_user.id,
                MeterReading.status == "pending",
            )
            .order_by(MeterReading.id.desc())
            .first()
        )
        result.append({
            "id": m.id,
            "meter_number": m.meter_number,
            "meter_type": m.meter_type,
            "shop_id": m.shop_id,
            "shop_number": shop.shop_number if shop else None,
            "previous_reading": float(meter_service.previous_reading_value(db, m)),
            "pending_reading_id": pending.id if pending else None,
            "has_pending": pending is not None,
        })
    return result


@router.post("/api/tenant/meter-readings", tags=["Tenant"], status_code=201)
async def submit_meter_reading(
    meter_id: int = Form(...),
    customer_reading: float = Form(...),
    customer_note: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """
    Tenant submits a meter photo and the reading they can see on it.

    Nothing is billed here - the submission simply enters the admin's review
    queue. Sent as multipart/form-data so the photo and the reading arrive in
    one request (no separate upload step for the tenant to get wrong).
    """
    meter = db.query(Meter).filter(Meter.id == meter_id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")
    if not meter.is_active:
        raise HTTPException(400, detail="This meter is no longer in use.")

    # Ownership: the meter must be on a shop assigned to THIS tenant. Without
    # this an ID in the form body would let anyone submit against any meter.
    # An unassigned meter (shop_id NULL) belongs to nobody, so it fails here too.
    if meter.shop_id is None or meter.shop_id not in _tenant_shop_ids(db, current_user.id):
        raise HTTPException(403, detail="This meter is not on one of your shops.")

    # One open submission per meter, otherwise the review queue fills with
    # duplicates of the same month and the previous-reading basis gets murky.
    existing_pending = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == meter.id, MeterReading.status == "pending")
        .first()
    )
    if existing_pending:
        raise HTTPException(
            409,
            detail="A reading for this meter is already waiting for admin review. "
                   "Please wait for it to be checked before sending another.",
        )

    cfg = settings_service.get_all(db)
    previous = meter_service.previous_reading_value(db, meter)
    current_value = Decimal(str(customer_reading))

    if current_value < 0:
        raise HTTPException(400, detail="Reading cannot be negative.")
    if current_value < previous:
        raise HTTPException(
            400,
            detail=f"The reading you entered ({current_value}) is lower than the last "
                   f"approved reading ({previous}). Please check the meter and try again.",
        )

    # ── Photo ──
    photo_key = photo_name = photo_mime = None
    photo_size = None
    photo_bytes = None

    if photo is not None and photo.filename:
        photo_bytes = await photo.read()
        try:
            ext, mime = photo_storage.validate(
                photo_bytes,
                photo.filename,
                str(cfg.get("meter.photo_allowed_types")),
                int(cfg.get("meter.photo_max_mb")),
            )
        except photo_storage.PhotoValidationError as exc:
            raise HTTPException(400, detail=str(exc))
        photo_key = photo_storage.build_key(meter.shop_id, meter.id, ext)
        photo_name = photo_storage.safe_original_name(photo.filename)
        photo_mime = mime
        photo_size = len(photo_bytes)
    elif cfg.get("meter.photo_required"):
        raise HTTPException(400, detail="A photo of the meter is required.")

    now = datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)
    reading = MeterReading(
        meter_id            = meter.id,
        shop_id             = meter.shop_id,
        user_id             = current_user.id,
        previous_reading    = previous,
        customer_reading    = current_value,
        customer_note       = (customer_note or "").strip() or None,
        photo_path          = photo_key,
        photo_original_name = photo_name,
        photo_size_bytes    = photo_size,
        photo_mime          = photo_mime,
        reading_date        = now,
        status              = "pending",
    )
    db.add(reading)
    db.flush()

    # Write the file only after the row is valid, so a rejected submission
    # never leaves an orphaned photo on disk.
    if photo_key:
        try:
            photo_storage.save(str(cfg.get("meter.photo_storage_dir")), photo_key, photo_bytes)
        except photo_storage.PhotoStorageError as exc:
            db.rollback()
            logger.error("Meter photo save failed for user %s: %s", current_user.id, exc)
            raise HTTPException(500, detail=str(exc))

    write_audit(db, current_user.id, "SUBMIT", "meter_readings", reading.id, new_data={
        "meter_id": meter.id, "customer_reading": float(current_value),
        "previous_reading": float(previous), "has_photo": bool(photo_key),
    })
    db.commit()
    db.refresh(reading)

    return {
        "success": True,
        "message": "Reading submitted. An admin will check your photo and confirm it.",
        "reading": _reading_to_dict(db, reading),
    }


@router.get("/api/tenant/meter-readings", tags=["Tenant"])
def tenant_meter_readings(
    status_filter: Optional[str] = Query(None, alias="status",
                                         pattern="^(pending|approved|rejected)$"),
    meter_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """The signed-in tenant's own submissions, newest first."""
    q = db.query(MeterReading).filter(MeterReading.user_id == current_user.id)
    if status_filter:
        q = q.filter(MeterReading.status == status_filter)
    if meter_id is not None:
        q = q.filter(MeterReading.meter_id == meter_id)
    rows = q.order_by(MeterReading.reading_date.desc(), MeterReading.id.desc()).all()
    return [_reading_to_dict(db, r) for r in rows]


@router.get("/api/tenant/meter-readings/{id}", tags=["Tenant"])
def tenant_meter_reading_detail(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    reading = db.query(MeterReading).filter(MeterReading.id == id).first()
    if not reading:
        raise HTTPException(404, detail="Reading not found")
    # IDOR guard: 404 (not 403) so the response can't be used to probe which
    # reading IDs exist for other tenants.
    if reading.user_id != current_user.id:
        raise HTTPException(404, detail="Reading not found")
    return _reading_to_dict(db, reading)


@router.get("/api/tenant/meters/{meter_id}/readings", tags=["Tenant"])
def tenant_meter_readings_paginated(
    meter_id: int,
    page:   int            = Query(1,  ge=1),
    limit:  int            = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status",
                                         pattern="^(pending|approved|rejected)$"),
    db:     Session        = Depends(get_db),
    current_user: User     = Depends(require_tenant),
):
    """
    One meter's own reading history, paginated - newest first.

    This is additive: it exists alongside GET /api/tenant/meter-readings
    (which returns everything across every meter the tenant has, unbounded)
    and the /api/tenant/home bundle (whose "readings" list is capped at the
    24 most recent across ALL of a tenant's meters combined, so a tenant
    with several submeters can have one meter's history crowd out another's
    there). Neither of those changes - existing callers keep working exactly
    as before. This endpoint is for a UI that lets a tenant open ONE meter at
    a time and page through JUST that meter's history, without pulling every
    other meter's readings along with it and without the crowding problem
    above.

    Pagination follows the same page/limit/total convention already used by
    GET /api/bills and GET /api/payments (see list_bills_paginated), just
    scoped to one tenant's one meter instead of being admin-only.
    """
    meter = db.query(Meter).filter(Meter.id == meter_id).first()
    # 404 either way (never 403) so this can't be used to probe which meter
    # IDs exist on shops that aren't the caller's - same IDOR guard used by
    # tenant_meter_reading_detail above.
    if not meter or meter.shop_id is None or meter.shop_id not in _tenant_shop_ids(db, current_user.id):
        raise HTTPException(404, detail="Meter not found")

    q = db.query(MeterReading).filter(
        MeterReading.meter_id == meter_id,
        MeterReading.user_id == current_user.id,
    )
    if status_filter:
        q = q.filter(MeterReading.status == status_filter)

    total  = q.count()
    offset = (page - 1) * limit
    rows = (
        q.order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    shop = db.query(Shop).filter(Shop.id == meter.shop_id).first()

    return {
        "success": True,
        "page":  page,
        "limit": limit,
        "total": total,
        "meter": {
            "id": meter.id,
            "meter_number": meter.meter_number,
            "meter_type": meter.meter_type,
            "shop_id": meter.shop_id,
            "shop_number": shop.shop_number if shop else None,
        },
        "data": [_reading_to_dict(db, r) for r in rows],
    }
