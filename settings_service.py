"""
settings_service.py - Runtime application configuration

Everything the admin might reasonably want to change without a redeploy lives
here: branding/labels, upload limits, billing behaviour and the submeter
review thresholds.

How it works:
    - DEFAULTS below is the single source of truth for the *shape* of the
      config: key, type, default value, category and help text.
    - A row only appears in the app_settings table once an admin has actually
      overridden that key. Anything not overridden falls back to DEFAULTS,
      which means adding a new setting in a future release Just Works on an
      existing database with no migration.
    - Values are cached in memory and invalidated on write, so reading config
      on a hot path (e.g. every upload) does not hit the database each time.

Environment variables still win where they exist (e.g. DB credentials, JWT
secret) - those are deployment concerns and deliberately NOT editable from the
frontend. This module is only for business/UX configuration.
"""

import threading
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from log import get_logger

logger = get_logger("app")


# ══════════════════════════════════════════════════════════════════════════════
# DEFAULTS
# type is one of: str | int | float | bool
# ══════════════════════════════════════════════════════════════════════════════

DEFAULTS: Dict[str, Dict[str, Any]] = {
    # ── Branding / labels ───────────────────────────────────────────────
    "app.name": {
        "value": "Ledger", "type": "str", "category": "Branding",
        "label": "Application name",
        "help": "Shown in the sidebar, page title and on generated PDFs.",
    },
    "app.tagline": {
        "value": "Shop & Tenant Management", "type": "str", "category": "Branding",
        "label": "Tagline",
        "help": "Short line shown under the application name.",
    },
    "app.currency_symbol": {
        "value": "₹", "type": "str", "category": "Branding",
        "label": "Currency symbol",
        "help": "Used everywhere amounts are displayed.",
    },
    "app.support_contact": {
        "value": "", "type": "str", "category": "Branding",
        "label": "Support contact",
        "help": "Phone or email shown to tenants who need help signing in.",
    },
    "label.tenant_singular": {
        "value": "Tenant", "type": "str", "category": "Branding",
        "label": "Word for a tenant",
        "help": "Change to 'Customer', 'Shopkeeper' etc. if that suits your business.",
    },
    "label.shop_singular": {
        "value": "Shop", "type": "str", "category": "Branding",
        "label": "Word for a shop",
        "help": "Change to 'Unit', 'Stall' etc.",
    },
    "label.complex_singular": {
        "value": "Complex", "type": "str", "category": "Branding",
        "label": "Word for a complex",
        "help": "Change to 'Property', 'Building' etc.",
    },

    # ── Meter photo uploads ─────────────────────────────────────────────
    "meter.photo_max_mb": {
        "value": 10, "type": "int", "category": "Meter readings",
        "label": "Max photo size (MB)",
        "help": "Uploads larger than this are rejected.",
    },
    "meter.photo_allowed_types": {
        "value": "jpg,jpeg,png,webp", "type": "str", "category": "Meter readings",
        "label": "Allowed photo types",
        "help": "Comma-separated file extensions the tenant may upload.",
    },
    "meter.photo_storage_dir": {
        "value": "uploads/meter-photos", "type": "str", "category": "Meter readings",
        "label": "Photo storage folder",
        "help": "Server folder where meter photos are kept. Photos are served only "
                "through an authorised endpoint, never as public files.",
    },
    "meter.photo_required": {
        "value": True, "type": "bool", "category": "Meter readings",
        "label": "Photo required on submission",
        "help": "When on, a tenant cannot submit a reading without attaching a photo.",
    },

    # ── Review thresholds (warnings only - the admin always decides) ─────
    "meter.high_consumption_units": {
        "value": 1000, "type": "int", "category": "Meter readings",
        "label": "High consumption warning (units)",
        "help": "Flags the submission for extra attention above this many units. "
                "It is only a warning - the admin can still approve.",
    },
    "meter.high_consumption_multiplier": {
        "value": 3.0, "type": "float", "category": "Meter readings",
        "label": "Spike warning multiplier",
        "help": "Warn when consumption is this many times the meter's recent average. "
                "Set to 0 to switch the check off.",
    },
    "meter.warn_zero_consumption": {
        "value": True, "type": "bool", "category": "Meter readings",
        "label": "Warn on zero consumption",
        "help": "Flag readings identical to the previous approved reading.",
    },
    "meter.require_override_reason": {
        "value": True, "type": "bool", "category": "Meter readings",
        "label": "Require reason when admin differs from tenant",
        "help": "When on, the admin must type a reason if their verified reading "
                "does not match what the tenant submitted.",
    },

    # ── Billing ─────────────────────────────────────────────────────────
    "meter.bill_type_label": {
        "value": "Electricity", "type": "str", "category": "Billing",
        "label": "Bill type for meter bills",
        "help": "The bill_type used for bills generated from an approved reading.",
    },
    "meter.bill_due_days": {
        "value": 15, "type": "int", "category": "Billing",
        "label": "Payment window (days)",
        "help": "Due date on a generated meter bill = approval date + this many days.",
    },
    "meter.auto_create_bill": {
        "value": True, "type": "bool", "category": "Billing",
        "label": "Create the bill automatically on approval",
        "help": "When off, approving a reading records the verified value but does "
                "not raise a bill (useful if you bill electricity outside the app).",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# CACHE
# ══════════════════════════════════════════════════════════════════════════════

_cache: Optional[Dict[str, Any]] = None
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    """Drop the in-memory cache; next read reloads from the database."""
    global _cache
    with _cache_lock:
        _cache = None


def _coerce(raw: Any, type_name: str) -> Any:
    """Convert a stored string back into its declared Python type."""
    if raw is None:
        return None
    if type_name == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(raw))
    if type_name == "float":
        return float(raw)
    return str(raw)


def _load(db: Session) -> Dict[str, Any]:
    """Build the full settings dict: defaults overlaid with DB overrides."""
    from create_tables import AppSetting  # imported lazily to avoid a cycle

    values = {key: spec["value"] for key, spec in DEFAULTS.items()}
    try:
        for row in db.query(AppSetting).all():
            if row.key not in DEFAULTS:
                continue  # stale key from an older release - ignore
            try:
                values[row.key] = _coerce(row.value, DEFAULTS[row.key]["type"])
            except (TypeError, ValueError):
                logger.warning(
                    "Setting %s has an unusable stored value (%r); using default.",
                    row.key, row.value,
                )
    except Exception as exc:
        # A missing table on a not-yet-migrated database must never take the
        # API down - fall back to defaults and carry on.
        logger.warning("Could not read app_settings (%s); using defaults.", exc)
    return values


def get_all(db: Session) -> Dict[str, Any]:
    """Every setting, resolved. Cached until something is written."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _load(db)
        return dict(_cache)


def get(db: Session, key: str) -> Any:
    """One resolved setting value. Unknown keys raise KeyError."""
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    return get_all(db).get(key, DEFAULTS[key]["value"])


def describe() -> list:
    """
    The settings schema for the admin UI: key, label, help, type, category and
    the factory default (so the UI can offer a 'reset to default' action).
    """
    return [
        {
            "key": key,
            "label": spec["label"],
            "help": spec["help"],
            "type": spec["type"],
            "category": spec["category"],
            "default": spec["value"],
        }
        for key, spec in DEFAULTS.items()
    ]


def set_many(db: Session, updates: Dict[str, Any], actor_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Persist a batch of settings. Validates every key and value BEFORE writing
    anything, so a bad value in the batch cannot leave a half-applied config.

    Returns {key: (old, new)} for the values that actually changed, which the
    caller writes into the audit log.
    """
    from create_tables import AppSetting

    unknown = [k for k in updates if k not in DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown setting key(s): {', '.join(sorted(unknown))}")

    # Validate/normalise first.
    normalised: Dict[str, Any] = {}
    for key, raw in updates.items():
        spec = DEFAULTS[key]
        try:
            normalised[key] = _coerce(raw, spec["type"])
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' must be of type {spec['type']}")

    _validate(normalised)

    current = get_all(db)
    changed: Dict[str, Any] = {}

    for key, value in normalised.items():
        if current.get(key) == value:
            continue
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row is None:
            row = AppSetting(key=key, value=str(value), updated_by=actor_id)
            db.add(row)
        else:
            row.value = str(value)
            row.updated_by = actor_id
        changed[key] = {"old": current.get(key), "new": value}

    if changed:
        db.flush()
        invalidate_cache()
    return changed


def _validate(values: Dict[str, Any]) -> None:
    """Guard rails on the values that could break the app if set nonsensically."""
    if "meter.photo_max_mb" in values and not (1 <= values["meter.photo_max_mb"] <= 50):
        raise ValueError("Max photo size must be between 1 and 50 MB")
    if "meter.bill_due_days" in values and not (0 <= values["meter.bill_due_days"] <= 365):
        raise ValueError("Payment window must be between 0 and 365 days")
    if "meter.high_consumption_units" in values and values["meter.high_consumption_units"] < 0:
        raise ValueError("High consumption warning cannot be negative")
    if "meter.high_consumption_multiplier" in values and values["meter.high_consumption_multiplier"] < 0:
        raise ValueError("Spike warning multiplier cannot be negative")
    if "meter.photo_allowed_types" in values:
        exts = [e.strip().lower() for e in str(values["meter.photo_allowed_types"]).split(",") if e.strip()]
        if not exts:
            raise ValueError("At least one photo type must be allowed")
        unsupported = [e for e in exts if e not in ("jpg", "jpeg", "png", "webp")]
        if unsupported:
            raise ValueError(
                f"Unsupported photo type(s): {', '.join(unsupported)}. "
                "Supported: jpg, jpeg, png, webp"
            )
    if "app.name" in values and not str(values["app.name"]).strip():
        raise ValueError("Application name cannot be empty")
