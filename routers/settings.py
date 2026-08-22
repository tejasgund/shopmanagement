"""
routers/settings.py - GET/PUT /api/settings, POST /api/settings/reset
(Admin only).

Extracted verbatim from app.py (step 13 of the router/service split).

NOTE: GET /api/settings/public deliberately stays in app.py, not here - it
calls _razorpay_public_config(), which reads the RAZORPAY_KEY_ID_ENV/
RAZORPAY_KEY_SECRET_ENV module globals that several tests monkeypatch
directly via app_module (see the same reasoning documented next to
_razorpay_credentials/_razorpay_webhook_secret/_razorpay_public_config in
app.py). Moving that one route here would silently break that patching.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import AppSetting, User
from auth_service import require_admin
from audit_service import write_audit
from schemas import SettingsUpdateRequest
import settings_service

router = APIRouter(tags=["Settings"])


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
    schema = settings_service.describe()
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
