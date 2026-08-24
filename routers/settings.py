"""
routers/settings.py - GET /api/settings/public (unauthenticated), GET/PUT
/api/settings, POST /api/settings/reset (Admin only).

Extracted verbatim from app.py (step 13 of the router/service split;
public_settings joined it in step 24, once services/razorpay.py gave
_razorpay_public_config() a stable home outside app.py).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.database import get_db
from core.security import require_admin
from models.schema import AppSetting, User
from schemas.api import SettingsUpdateRequest
from services import settings as settings_service
from services.audit import write_audit
from services.razorpay import _razorpay_public_config


router = APIRouter(tags=["Settings"])


@router.get("/api/settings/public", tags=["Settings"])
def public_settings(db: Session = Depends(get_db)):
    """
    The handful of settings the sign-in page and both portals need before a
    user is authenticated (app name, tagline, currency symbol, support
    contact). Deliberately a small allow-list - no configuration that isn't
    already visible on screen is exposed here.
    """
    cfg = settings_service.get_all(db)
    razorpay_enabled, razorpay_key_id = _razorpay_public_config(cfg)
    return {
        "app_name": cfg.get("app.name"),
        "tagline": cfg.get("app.tagline"),
        "currency_symbol": cfg.get("app.currency_symbol"),
        "support_contact": cfg.get("app.support_contact"),
        "labels": {
            "tenant": cfg.get("label.tenant_singular"),
            "shop": cfg.get("label.shop_singular"),
            "complex": cfg.get("label.complex_singular"),
        },
        "razorpay_enabled": razorpay_enabled,
        "razorpay_key_id": razorpay_key_id,
    }


@router.get("/api/settings", tags=["Settings"])
def get_settings(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    Every setting with its current value, type, help text and factory default.

    "secret"-type settings (e.g. the Razorpay Key Secret) never have their
    real value sent to the browser here - only whether one is currently set
    (`is_set`) - so opening Settings, or a browser network trace, can't leak
    it. The admin re-types the value only when they actually want to change
    it; a blank field means "leave it as-is" (see settings_service.set_many).
    """
    values = settings_service.get_all(db)
    # Scheduler settings are owned by the Scheduler app and are served by
    # GET /api/scheduler/settings instead - see settings_service.describe_for.
    schema = settings_service.describe_for("main")
    for item in schema:
        real_value = values.get(item["key"], item["default"])
        if item["type"] == "secret":
            item["is_set"] = bool(real_value)
            item["value"] = ""
        else:
            item["value"] = real_value
    return {
        "categories": sorted({item["category"] for item in schema}),
        "settings": schema,
    }


@router.put("/api/settings", tags=["Settings"])
def update_settings(
    payload: SettingsUpdateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Update one or more settings. The whole batch is validated before anything
    is written, so an invalid value can't leave the config half-applied.
    """
    if not payload.values:
        raise HTTPException(400, detail="No settings were provided")

    # Refused rather than quietly filtered: a caller trying to set a scheduler
    # key here has the wrong endpoint, and silently dropping it would look like
    # a save that worked.
    intruders = sorted(k for k in payload.values if settings_service.is_scheduler_key(k))
    if intruders:
        raise HTTPException(
            400,
            detail="Scheduler settings are managed in the Scheduler app, not here: "
                   + ", ".join(intruders),
        )

    try:
        changed = settings_service.set_many(db, payload.values, actor_id=actor.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, detail=str(exc))

    if changed:
        write_audit(db, actor.id, "UPDATE", "app_settings", None, new_data=changed)
    db.commit()
    settings_service.invalidate_cache()

    return {
        "success": True,
        "message": f"{len(changed)} setting(s) updated" if changed else "No changes to save",
        "changed": changed,
    }


@router.post("/api/settings/reset", tags=["Settings"])
def reset_settings(
    keys: Optional[List[str]] = None,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """Restore settings to their factory defaults (all, or just the keys given)."""
    q = db.query(AppSetting)
    if keys:
        q = q.filter(AppSetting.key.in_(keys))
    # "Reset all" belongs to this app only - it must never silently wipe the
    # Scheduler app's configuration along with its own.
    q = q.filter(~AppSetting.key.like(f"{settings_service.SCHEDULER_PREFIX}%"))
    rows = q.all()
    removed = [r.key for r in rows]
    for row in rows:
        db.delete(row)

    if removed:
        write_audit(db, actor.id, "RESET", "app_settings", None, old_data={"keys": removed})
    db.commit()
    settings_service.invalidate_cache()
    return {"success": True, "message": f"{len(removed)} setting(s) reset to default",
            "reset": removed}
