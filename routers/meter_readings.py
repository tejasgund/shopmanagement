"""
routers/meter_readings.py - the admin meter-reading review workflow:
photo serving, admin-collected submissions, the review queue, preview/verify/
approve/reject.

Extracted verbatim from app.py (step 12 of the router/service split).
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Meter, MeterReading, Shop, User, UserShop
from auth_service import get_current_user, require_admin
from audit_service import write_audit
from domain_helpers import _decimal_to_float
from meter_helpers import _meter_error, _reading_to_dict, _readings_to_dicts
from app_config import APP_TIMEZONE
from log import get_logger
from schemas import ApproveReadingRequest, RejectReadingRequest, VerifyReadingRequest
import meter_service
import photo_storage
import settings_service
from meter_service import MeterError

logger = get_logger("app")

router = APIRouter(tags=["Meter"])


@router.get("/api/meter-readings/{id}/photo", tags=["Meter"])
def get_meter_photo(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Stream the original evidence photo.

    This is the ONLY way to see a meter photo - files are stored outside any
    static directory and never given a public URL. An admin may view any photo;
    a tenant may view only their own.
    """
    reading = db.query(MeterReading).filter(MeterReading.id == id).first()
    if not reading:
        raise HTTPException(404, detail="Reading not found")

    if current_user.role != "admin" and reading.user_id != current_user.id:
        raise HTTPException(404, detail="Reading not found")

    if not reading.photo_path:
        raise HTTPException(404, detail="No photo was attached to this reading")

    cfg = settings_service.get_all(db)
    try:
        content = photo_storage.read(str(cfg.get("meter.photo_storage_dir")), reading.photo_path)
    except photo_storage.PhotoStorageError as exc:
        raise HTTPException(404, detail=str(exc))

    return Response(
        content=content,
        media_type=reading.photo_mime or "image/jpeg",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="meter-{reading.id}.jpg"',
        },
    )


@router.post("/api/meter-readings/collect", tags=["Meter"], status_code=201)
async def collect_meter_reading(
    meter_id: int = Form(...),
    customer_reading: float = Form(...),
    customer_note: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Admin submits a reading on a tenant's behalf (e.g. the tenant can't use
    the app). Mirrors submit_meter_reading() above almost exactly - same
    validation, same photo handling, same "pending" starting status - so it
    lands in the same review queue an ordinary tenant submission would.

    The only differences: user_id is resolved from the meter's assigned
    tenant (never the admin - everything downstream, including the tenant's
    own "My Readings" list, keys off user_id), and collected_by records
    which admin sent it in for audit purposes.
    """
    meter = db.query(Meter).filter(Meter.id == meter_id).first()
    if not meter:
        raise HTTPException(404, detail="Meter not found")
    if not meter.is_active:
        raise HTTPException(400, detail="This meter is no longer in use.")
    if meter.shop_id is None:
        raise HTTPException(400, detail="This meter isn't assigned to a shop yet.")

    user_shop = (
        db.query(UserShop)
        .filter(UserShop.shop_id == meter.shop_id)
        .order_by(UserShop.assigned_at.desc())
        .first()
    )
    if not user_shop:
        raise HTTPException(400, detail="This meter's shop has no tenant assigned yet.")
    tenant_id = user_shop.user_id

    # Same one-open-submission-per-meter rule as the tenant path.
    existing_pending = (
        db.query(MeterReading)
        .filter(MeterReading.meter_id == meter.id, MeterReading.status == "pending")
        .first()
    )
    if existing_pending:
        raise HTTPException(
            409,
            detail="A reading for this meter is already waiting for review. "
                   "Please approve or reject it before collecting another.",
        )

    cfg = settings_service.get_all(db)
    previous = meter_service.previous_reading_value(db, meter)
    current_value = Decimal(str(customer_reading))

    if current_value < 0:
        raise HTTPException(400, detail="Reading cannot be negative.")
    if current_value < previous:
        raise HTTPException(
            400,
            detail=f"The reading entered ({current_value}) is lower than the last "
                   f"approved reading ({previous}). Please check the meter and try again.",
        )

    # ── Photo (same requirement rule as the tenant flow) ──
    # Gated by the ADMIN switch only - an admin can still attach a photo when
    # tenant uploads are turned off, and vice versa. Deliberately no window
    # check anywhere in this endpoint: the tenant submission window must never
    # stop an admin recording a reading on someone's behalf.
    photo_allowed = meter_service.photo_upload_allowed(cfg, meter_service.ADMIN)
    photo_key = photo_name = photo_mime = None
    photo_size = None
    photo_bytes = None

    if photo is not None and photo.filename and not photo_allowed:
        raise HTTPException(
            400,
            detail="Photo upload is currently turned off. Please record just the "
                   "meter reading, without a photo.",
        )

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
    elif meter_service.photo_required(cfg, meter_service.ADMIN):
        raise HTTPException(400, detail="A photo of the meter is required.")

    now = datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)
    reading = MeterReading(
        meter_id            = meter.id,
        shop_id             = meter.shop_id,
        user_id             = tenant_id,
        collected_by        = current_user.id,
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
            logger.error("Meter photo save failed (admin-collected) for meter %s: %s", meter.id, exc)
            raise HTTPException(500, detail=str(exc))

    write_audit(db, current_user.id, "COLLECT", "meter_readings", reading.id, new_data={
        "meter_id": meter.id, "customer_reading": float(current_value),
        "previous_reading": float(previous), "has_photo": bool(photo_key),
        "tenant_user_id": tenant_id,
    })
    db.commit()
    db.refresh(reading)

    return {
        "success": True,
        "message": "Reading saved — waiting for review in Meter Readings.",
        "reading": _reading_to_dict(db, reading, include_admin_fields=True),
    }


@router.get("/api/meter-readings", tags=["Meter"])
def list_meter_readings(
    status_filter: Optional[str] = Query(None, alias="status",
                                         pattern="^(pending|approved|rejected)$"),
    meter_id:   Optional[int] = None,
    shop_id:    Optional[int] = None,
    user_id:    Optional[int] = None,
    complex_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    The review queue. Defaults to every status; pass ?status=pending for the
    admin's actual to-do list. Oldest pending first so nobody is left waiting.
    """
    q = db.query(MeterReading)
    if status_filter:
        q = q.filter(MeterReading.status == status_filter)
    if meter_id is not None:
        q = q.filter(MeterReading.meter_id == meter_id)
    if shop_id is not None:
        q = q.filter(MeterReading.shop_id == shop_id)
    if user_id is not None:
        q = q.filter(MeterReading.user_id == user_id)
    if complex_id is not None:
        q = q.join(Shop, Shop.id == MeterReading.shop_id).filter(Shop.complex_id == complex_id)

    if status_filter == "pending":
        q = q.order_by(MeterReading.reading_date.asc(), MeterReading.id.asc())
    else:
        q = q.order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())

    return _readings_to_dicts(db, q.all(), include_admin_fields=True)


# NOTE: this concrete path MUST be registered before "/api/meter-readings/{id}"
# so FastAPI doesn't try to parse "paginated" as an integer id (same reason
# /api/complex/summary is registered before /api/complex/{id}).
@router.get("/api/meter-readings/paginated", tags=["Meter"])
def list_meter_readings_paginated(
    page:       int            = Query(1,  ge=1),
    limit:      int            = Query(20, ge=1, le=200),
    status_filter: Optional[str] = Query(None, alias="status",
                                         pattern="^(pending|approved|rejected)$"),
    meter_id:   Optional[int] = None,
    shop_id:    Optional[int] = None,
    user_id:    Optional[int] = None,
    complex_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Paginated version of the review queue. Additive alongside
    GET /api/meter-readings (which returns everything, unbounded, and stays
    exactly as-is) - same page/limit/total convention as GET /api/bills and
    GET /api/payments. Same filters and default ordering as the unpaginated
    endpoint (oldest pending first when status=pending, newest first
    otherwise). Admin only.
    """
    q = db.query(MeterReading)
    if status_filter:
        q = q.filter(MeterReading.status == status_filter)
    if meter_id is not None:
        q = q.filter(MeterReading.meter_id == meter_id)
    if shop_id is not None:
        q = q.filter(MeterReading.shop_id == shop_id)
    if user_id is not None:
        q = q.filter(MeterReading.user_id == user_id)
    if complex_id is not None:
        q = q.join(Shop, Shop.id == MeterReading.shop_id).filter(Shop.complex_id == complex_id)

    total = q.count()

    if status_filter == "pending":
        q = q.order_by(MeterReading.reading_date.asc(), MeterReading.id.asc())
    else:
        q = q.order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())

    offset = (page - 1) * limit
    rows = q.offset(offset).limit(limit).all()

    return {
        "success": True,
        "page":  page,
        "limit": limit,
        "total": total,
        "data":  _readings_to_dicts(db, rows, include_admin_fields=True),
    }


@router.get("/api/meter-readings/{id}", tags=["Meter"])
def get_meter_reading(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    Everything the admin needs on one screen to verify a submission: the photo,
    the previous approved reading, what the tenant entered, the live tariff,
    and - once they type their own reading - the comparison and estimate.
    """
    reading = db.query(MeterReading).filter(MeterReading.id == id).first()
    if not reading:
        raise HTTPException(404, detail="Reading not found")

    meter = db.query(Meter).filter(Meter.id == reading.meter_id).first()
    data = _reading_to_dict(db, reading, include_admin_fields=True)

    previous = _decimal_to_float(reading.previous_reading)
    customer_value = Decimal(str(reading.customer_reading))

    # Provisional numbers based on the TENANT's reading, clearly labelled as
    # such. They preview what the bill would look like; they are never used to
    # bill anything - only the admin's own entry does that.
    provisional_units = None
    provisional_estimate = None
    if customer_value >= Decimal(str(previous)):
        provisional_units = float(customer_value - Decimal(str(previous)))
        provisional_estimate = meter_service.estimate_bill(
            db, meter, Decimal(str(provisional_units)), reading.reading_date,
        )

    anomalies = []
    if provisional_units is not None:
        anomalies = meter_service.detect_anomalies(
            db, meter, Decimal(str(previous)), customer_value, Decimal(str(provisional_units)),
        )

    cfg = settings_service.get_all(db)
    data["review"] = {
        "previous_reading": previous,
        "previous_reading_source": (
            "last approved reading" if meter_service.latest_approved_reading(db, meter.id)
            else "meter initial reading"
        ),
        "provisional_units_from_customer": provisional_units,
        "provisional_estimate_from_customer": provisional_estimate,
        "comparison": meter_service.build_comparison(
            reading.customer_reading, reading.admin_verified_reading,
        ),
        "anomalies": anomalies,
        "requires_override_reason": bool(cfg.get("meter.require_override_reason")),
        "auto_create_bill": bool(cfg.get("meter.auto_create_bill")),
        "note": (
            "The bill is calculated from YOUR verified reading, not from the "
            "tenant's entry. Check the photo before approving."
        ),
    }
    return data


@router.post("/api/meter-readings/{id}/preview", tags=["Meter"])
def preview_meter_reading(
    id: int,
    payload: VerifyReadingRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Dry run: what this reading would produce if approved at the admin's value.
    Changes nothing - it powers the live comparison/estimate on the review
    screen as the admin types, so they see the bill before committing to it.
    """
    reading = db.query(MeterReading).filter(MeterReading.id == id).first()
    if not reading:
        raise HTTPException(404, detail="Reading not found")

    meter = db.query(Meter).filter(Meter.id == reading.meter_id).first()
    previous = Decimal(str(reading.previous_reading))
    admin_value = Decimal(str(payload.admin_verified_reading))

    try:
        units = meter_service.calculate_units(previous, admin_value)
    except MeterError as exc:
        return {
            "valid": False,
            "error": exc.message,
            "comparison": meter_service.build_comparison(reading.customer_reading, admin_value),
        }

    estimate = meter_service.estimate_bill(db, meter, units, reading.reading_date)
    return {
        "valid": estimate["error"] is None,
        "error": estimate["error"],
        "previous_reading": float(previous),
        "admin_verified_reading": float(admin_value),
        "units": float(units),
        "estimate": estimate,
        "comparison": meter_service.build_comparison(reading.customer_reading, admin_value),
        "anomalies": meter_service.detect_anomalies(db, meter, previous, admin_value, units),
    }


@router.patch("/api/meter-readings/{id}/verify", tags=["Meter"])
def verify_meter_reading(
    id: int,
    payload: VerifyReadingRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Save the admin's reading without approving yet - useful when they want to
    record what they read but check something else before billing.
    """
    reading = db.query(MeterReading).filter(MeterReading.id == id).first()
    if not reading:
        raise HTTPException(404, detail="Reading not found")
    if reading.status != "pending":
        raise HTTPException(409, detail=f"This reading is already {reading.status}.")

    old_value = _decimal_to_float(reading.admin_verified_reading) if reading.admin_verified_reading is not None else None
    reading.admin_verified_reading = Decimal(str(payload.admin_verified_reading))
    reading.admin_verified_by = actor.id
    reading.admin_verified_at = datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)
    reading.admin_note = (payload.admin_note or "").strip() or None

    write_audit(db, actor.id, "VERIFY", "meter_readings", reading.id,
                old_data={"admin_verified_reading": old_value},
                new_data={"admin_verified_reading": float(reading.admin_verified_reading)})
    db.commit()
    db.refresh(reading)
    return _reading_to_dict(db, reading, include_admin_fields=True)


@router.post("/api/meter-readings/{id}/approve", tags=["Meter"])
def approve_meter_reading(
    id: int,
    payload: ApproveReadingRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Approve the reading and raise the bill, in one transaction.

    The admin's verified reading becomes the approved (billable) reading. If
    anything fails - a missing tariff, a reading below the previous one - the
    whole thing rolls back, so a reading is never left approved without its
    bill. Approving twice is refused, and meter_readings.bill_id is UNIQUE, so
    a double-clicked button cannot create a second bill.
    """
    # SELECT ... FOR UPDATE where the database supports it, so two admins
    # clicking Approve at the same moment serialise instead of racing.
    q = db.query(MeterReading).filter(MeterReading.id == id)
    try:
        reading = q.with_for_update().first()
    except Exception:
        reading = q.first()   # SQLite and friends - the status check still guards us

    if not reading:
        raise HTTPException(404, detail="Reading not found")

    meter = db.query(Meter).filter(Meter.id == reading.meter_id).first()
    if not meter:
        raise HTTPException(404, detail="The meter for this reading no longer exists")

    old_state = {"status": reading.status,
                 "customer_reading": _decimal_to_float(reading.customer_reading)}

    try:
        result = meter_service.approve_reading(
            db, reading, meter,
            admin_reading   = Decimal(str(payload.admin_verified_reading)),
            admin_id        = actor.id,
            override_reason = payload.override_reason,
            admin_note      = payload.admin_note,
        )

        write_audit(db, actor.id, "APPROVE", "meter_readings", reading.id,
                    old_data=old_state,
                    new_data={
                        "status": "approved",
                        "approved_reading": result["approved_reading"],
                        "previous_reading": result["previous_reading"],
                        "units": result["units"],
                        "bill_id": result["bill_id"],
                        "override": result["override"],
                        "override_reason": reading.override_reason,
                    })
        if result["bill_created"]:
            write_audit(db, actor.id, "CREATE", "bills", result["bill_id"], new_data={
                "source": "meter_reading", "meter_reading_id": reading.id,
                "amount": result["bill_amount"], "units": result["units"],
                "unit_price": result.get("unit_price"),
            })
        db.commit()
    except MeterError as exc:
        db.rollback()
        raise _meter_error(exc)
    except Exception as exc:
        db.rollback()
        logger.exception("Approval failed for meter reading %s: %s", id, exc)
        raise HTTPException(500, detail="Could not approve the reading. Nothing was changed.")

    db.refresh(reading)
    return {
        "success": True,
        "message": (
            f"Approved. Bill of {result['bill_amount']} raised for {result['units']} units."
            if result["bill_created"]
            else f"Approved {result['units']} units. No bill was created "
                 f"(automatic billing is switched off in settings)."
        ),
        "reading": _reading_to_dict(db, reading, include_admin_fields=True),
        "result": result,
    }


@router.post("/api/meter-readings/{id}/reject", tags=["Meter"])
def reject_meter_reading(
    id: int,
    payload: RejectReadingRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Reject a submission - blurry photo, wrong meter, obviously wrong number.
    The reason is shown to the tenant so they know what to fix. The photo is
    kept for audit.
    """
    reading = db.query(MeterReading).filter(MeterReading.id == id).first()
    if not reading:
        raise HTTPException(404, detail="Reading not found")

    old_state = {"status": reading.status}
    try:
        meter_service.reject_reading(db, reading, actor.id, payload.reason)
        write_audit(db, actor.id, "REJECT", "meter_readings", reading.id,
                    old_data=old_state,
                    new_data={"status": "rejected", "rejection_reason": reading.rejection_reason})
        db.commit()
    except MeterError as exc:
        db.rollback()
        raise _meter_error(exc)

    db.refresh(reading)
    return {
        "success": True,
        "message": "Reading rejected. The tenant can now submit a new photo.",
        "reading": _reading_to_dict(db, reading, include_admin_fields=True),
    }
