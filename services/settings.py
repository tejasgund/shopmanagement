"""
services/settings.py - Runtime application configuration

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

Deliberate exception: the Razorpay key ID/secret ARE stored here (type
"secret"), admin-editable from Settings, rather than requiring a server-side
.env file. This trades a small amount of theoretical risk (an admin account
being able to read/rotate the live payment key) for a much simpler ops story
on deployments that replace files wholesale (Jenkins, etc.) where hand-editing
a .env on the box every release is impractical. A "secret"-typed value is
never echoed back by GET /api/settings (see app.py's get_settings()) - only
whether one is currently set - and a blank submission on PUT never clears an
existing one (see set_many() below).
"""

import os
import threading
import time
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from core.logger import get_logger

logger = get_logger("app")


# ══════════════════════════════════════════════════════════════════════════════
# DEFAULTS
# type is one of: str | int | float | bool | secret
# "secret" behaves exactly like "str" for storage/coercion - the only
# difference is at the API layer: never echoed back once set, and a blank
# submission leaves the stored value untouched instead of clearing it.
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
    "app.payment_methods": {
        "value": "Cash, UPI or bank transfer at the office.", "type": "str", "category": "Branding",
        "label": "How to pay (tenant portal)",
        "help": "Shown on the tenant portal's 'How to pay' screen. Methods only - "
                "never put an account number or UPI ID here, since this is sent to "
                "every tenant's browser.",
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
    # ── Who may attach a photo ───────────────────────────────────────────
    # These gate the EXISTING photo upload on the reading forms; they do not
    # add a second upload path. When a role's switch is off, that role's photo
    # field is hidden and a photo sent anyway is refused - but the reading
    # itself is still accepted, and "Photo required on submission" above is
    # ignored for that role (a required photo that cannot be attached would
    # otherwise lock the role out of submitting entirely).
    "meter.allow_admin_photo_upload": {
        "value": True, "type": "bool", "category": "Meter readings",
        "label": "Allow admin to upload meter reading image",
        "help": "When off, the photo field is hidden on the admin's Collect reading "
                "form. Admins can still record the reading itself.",
    },
    "meter.allow_tenant_photo_upload": {
        "value": True, "type": "bool", "category": "Meter readings",
        "label": "Allow tenant to upload meter reading image",
        "help": "When off, the photo field is hidden in the tenant portal. Tenants "
                "can still submit the reading itself.",
    },

    # Whether a TENANT's photo may come from their existing photos rather than
    # only from the camera at the moment of submission. Off by default so the
    # camera-only behaviour that shipped before this setting is unchanged until
    # an admin deliberately relaxes it. Admins collecting a reading always have
    # both options and are deliberately not affected by this at all.
    #
    # This is a UI control, not a security boundary: it works by dropping the
    # `capture` attribute from the file input, which is a hint browsers honour
    # on phones. The server cannot tell a photo taken seconds ago from one
    # picked out of the gallery - both arrive as the same JPEG - so this
    # decides what the app OFFERS, and cannot police what a determined caller
    # posts to the API directly.
    "meter.allow_gallery_upload": {
        "value": False, "type": "bool", "category": "Meter readings",
        "label": "Allow gallery upload (tenants)",
        "help": "Controls TENANTS only. Off: a tenant's reading photo must be "
                "taken with the camera there and then. On: a tenant may also "
                "choose an existing photo from their device, and the camera is "
                "still offered. Admins always have both, whatever this is set to.",
    },

    # ── When tenants may submit ──────────────────────────────────────────
    # A day-of-month window that repeats every month, so it is set once rather
    # than re-edited each cycle. Admins are never restricted by it.
    "meter.tenant_upload_any_day": {
        # Defaults OFF so that setting a window actually does something. It
        # used to default ON, which meant an admin could narrow the window to
        # 1-10, save, and have it silently ignored - the window looked broken
        # with nothing on screen to explain why. With the default range being
        # the whole month (1-31), off-by-default behaves identically to "every
        # day" on a fresh install while leaving the window live the moment it
        # is narrowed.
        "value": False, "type": "bool", "category": "Meter readings",
        "label": "Allow tenant reading upload every day (ignores the window below)",
        "help": "Leave OFF to use the day range below - that is what makes the "
                "window work. Turn ON only to suspend the window temporarily and "
                "let tenants submit on any day; while it is on, the From/To days "
                "below have no effect. Admins are never restricted either way.",
    },
    "meter.tenant_upload_from_day": {
        "value": 1, "type": "int", "category": "Meter readings",
        "label": "Tenant upload window - from day of month",
        "help": "First day of each month tenants may submit on (1-31). Only applies "
                "when 'every day' above is off. Admins are never restricted.",
    },
    "meter.tenant_upload_to_day": {
        "value": 31, "type": "int", "category": "Meter readings",
        "label": "Tenant upload window - to day of month",
        "help": "Last day of each month tenants may submit on (1-31). In a shorter "
                "month this is treated as that month's last day, so 31 always means "
                "'to month end'. Only applies when 'every day' above is off.",
    },

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
    "bill.due_days": {
        "value": 30, "type": "int", "category": "Billing",
        "label": "Default bill due period (days)",
        "help": "Due date for newly created bills = bill date + this many days. "
                "Admins can override the due date for any individual bill.",
    },

    # ══════════════════════════════════════════════════════════════════════
    # Scheduler
    #
    # Read by the two standalone scripts under scheduler/ - they query this
    # table directly. Declared here because this is the screen that edits
    # them, and because a switch in a config file on the server AND a toggle
    # in the UI would be two answers to the same question.
    #
    # The scripts hold the same defaults as a fallback for a database where a
    # key has never been customised. Change a default here and change it
    # there; the test in tests/test_scheduler_tracking.py checks they agree.
    #
    # A change takes effect on the scripts' NEXT run - they read these fresh
    # every time, so there is nothing to restart and no crontab to edit.
    # ══════════════════════════════════════════════════════════════════════
    "scheduler.rent_generation_enabled": {
        "value": True, "type": "bool", "category": "Scheduler",
        "label": "Automatic rent generation",
        "help": "Generate each tenant's monthly Rent bill on their configured "
                "bill day. Off means the cron job still runs but does nothing, "
                "and records that it was switched off.",
    },
    "scheduler.penalty_enabled": {
        "value": False, "type": "bool", "category": "Scheduler",
        "label": "Due-date penalty",
        "help": "Add a daily late fee to overdue unpaid bills. Off by default - "
                "turning it on starts charging tenants, so it is a deliberate "
                "decision rather than something that happens on upgrade.",
    },
    "scheduler.penalty_percent_per_day": {
        "value": 1.0, "type": "float", "category": "Scheduler",
        "label": "Penalty per day (% of the original bill)",
        "help": "1 = 1% of the ORIGINAL bill amount per chargeable day. A 10,000 "
                "bill at 1% accrues 100 a day. The percentage is always of the "
                "original, so a penalty never earns a penalty.",
    },
    "scheduler.penalty_grace_days": {
        "value": 0, "type": "int", "category": "Scheduler",
        "label": "Grace period (days)",
        "help": "Days after the due date before any penalty starts. 0 charges "
                "from the first day overdue; 5 means a bill due on the 10th "
                "starts accruing on the 16th.",
    },
    "scheduler.penalty_on_penalty_enabled": {
        "value": False, "type": "bool", "category": "Scheduler",
        "label": "Charge a late fee on unpaid late fees",
        "help": "Off (recommended): a late-fee bill never accrues a late fee of "
                "its own, so the amount a tenant owes stops growing once the "
                "original bill is settled. On: an overdue late-fee bill is "
                "charged like any other, which compounds a fee into a debt that "
                "can grow faster than a tenant can clear it. Off by default, and "
                "turning it on is a deliberate decision.",
    },
    "scheduler.penalty_max_amount": {
        "value": 0, "type": "float", "category": "Scheduler",
        "label": "Maximum penalty per bill",
        "help": "Upper limit on the accrued penalty for a single bill. 0 means "
                "no cap, and the penalty keeps growing until the bill is paid.",
    },

    # ── Online payments (Razorpay) ────────────────────────────────────────
    # The keys themselves (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET) are env-only,
    # never here - this is just the on/off switch and the tenant-facing copy.
    "payment.razorpay_enabled": {
        "value": False, "type": "bool", "category": "Online payments",
        "label": "Allow tenants to pay bills online",
        "help": "When on, tenants see a 'Pay online' / 'Pay bill' button on unpaid "
                "bills (Razorpay). Also requires a Key ID and Key Secret below (or "
                "in the server's .env as a fallback), or this stays off regardless.",
    },
    "payment.razorpay_key_id": {
        "value": "", "type": "str", "category": "Online payments",
        "label": "Razorpay Key ID",
        "help": "From your Razorpay dashboard -> Settings -> API Keys. Safe to "
                "expose to the browser (the checkout widget needs it) - it is not "
                "the secret.",
    },
    "payment.razorpay_key_secret": {
        "value": "", "type": "secret", "category": "Online payments",
        "label": "Razorpay Key Secret",
        "help": "From the same page as the Key ID. Never shown again once saved - "
                "leave this blank when saving other settings to keep it unchanged, "
                "or type a new value to replace it.",
    },
    "payment.razorpay_webhook_secret": {
        "value": "", "type": "secret", "category": "Online payments",
        "label": "Razorpay Webhook Secret",
        "help": "From Razorpay dashboard -> Settings -> Webhooks -> (this webhook) "
                "-> Secret. Different from the Key Secret above - this one is only "
                "used to verify that /api/webhooks/razorpay calls really came from "
                "Razorpay. Required for the webhook to work; leave blank when saving "
                "other settings to keep it unchanged.",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# CACHE
# ══════════════════════════════════════════════════════════════════════════════

_cache: Optional[Dict[str, Any]] = None
_cache_lock = threading.Lock()
_cache_loaded_at = 0.0

# The API runs under `uvicorn --workers 2`, and every worker is a separate
# process with its own copy of this module - so invalidate_cache() below only
# ever reaches the ONE worker that handled the write. Without an expiry the
# other workers keep serving whatever they cached at startup indefinitely: an
# admin changes the Razorpay keys (or the bill due period, or a meter setting)
# and roughly half of all requests carry on using the old value, which reads
# as the setting randomly not applying, or as intermittent gateway auth
# failures. A short TTL bounds that to a few seconds and costs one small query
# per worker per interval - far simpler than a cross-process signal, and the
# writing worker still updates instantly via invalidate_cache().
_CACHE_TTL_SECONDS = float(os.getenv("SETTINGS_CACHE_TTL_SECONDS", "10"))


def invalidate_cache() -> None:
    """Drop the in-memory cache; next read reloads from the database."""
    global _cache, _cache_loaded_at
    with _cache_lock:
        _cache = None
        _cache_loaded_at = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# APP OWNERSHIP
#
# Scheduler settings are stored in this same table - one settings mechanism,
# one validation path, one audit trail - but they belong to the Scheduler app
# and are deliberately NOT part of the main admin Settings screen. The split is
# enforced at the API layer (see routers/settings.py and
# routers/scheduler_admin.py) rather than merely hidden in the UI, so neither
# app can reach into the other's configuration even by crafting a request.
# ══════════════════════════════════════════════════════════════════════════════

SCHEDULER_PREFIX = "scheduler."


def is_scheduler_key(key: str) -> bool:
    return str(key).startswith(SCHEDULER_PREFIX)


def describe_for(app: str) -> list:
    """
    The settings schema belonging to one app: "scheduler" or "main".

    Keeps the ownership rule in one place instead of every endpoint
    re-deciding what it is allowed to show.
    """
    want_scheduler = app == "scheduler"
    return [item for item in describe() if is_scheduler_key(item["key"]) == want_scheduler]


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
    from models.schema import AppSetting  # imported lazily to avoid a cycle

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
    """
    Every setting, resolved. Cached until something is written in THIS process
    or the cache goes stale (see _CACHE_TTL_SECONDS for why the expiry exists).
    """
    global _cache, _cache_loaded_at
    with _cache_lock:
        stale = (time.monotonic() - _cache_loaded_at) >= _CACHE_TTL_SECONDS
        if _cache is None or stale:
            _cache = _load(db)
            _cache_loaded_at = time.monotonic()
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
    from models.schema import AppSetting

    unknown = [k for k in updates if k not in DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown setting key(s): {', '.join(sorted(unknown))}")

    # Validate/normalise first.
    normalised: Dict[str, Any] = {}
    for key, raw in updates.items():
        spec = DEFAULTS[key]
        # A blank "secret" means "leave it as-is". GET /api/settings never
        # echoes the real value back, so the admin's form always starts
        # blank - submitting the rest of the form unchanged must not wipe
        # out a secret that was never re-typed.
        if spec["type"] == "secret" and (raw is None or str(raw).strip() == ""):
            continue
        try:
            normalised[key] = _coerce(raw, spec["type"])
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' must be of type {spec['type']}")

    _validate(normalised, get_all(db))

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


def _validate(values: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> None:
    """
    Guard rails on the values that could break the app if set nonsensically.

    `current` is the config as it stands now. Rules that compare two settings
    against each other need it: the admin may be saving only one half of a
    pair, and the incoming half still has to make sense against the stored
    other half rather than passing simply because it wasn't in this batch.
    """
    if "meter.photo_max_mb" in values and not (1 <= values["meter.photo_max_mb"] <= 50):
        raise ValueError("Max photo size must be between 1 and 50 MB")
    if "meter.bill_due_days" in values and not (0 <= values["meter.bill_due_days"] <= 365):
        raise ValueError("Payment window must be between 0 and 365 days")
    if "bill.due_days" in values and not (0 <= values["bill.due_days"] <= 365):
        raise ValueError("Bill due period must be between 0 and 365 days")
    # Day-of-month window. Checked against the merged view of current + incoming
    # values, so saving just one of the two days is still validated against the
    # other's stored value rather than passing because it wasn't in this batch.
    # Scheduler bounds. These exist to stop a value that would make a nightly
    # run behave absurdly - a 500% daily penalty, a rate that turns a late fee
    # into a debt spiral - rather than to protect the UI.
    if "scheduler.penalty_percent_per_day" in values and not (
        0 <= float(values["scheduler.penalty_percent_per_day"]) <= 100
    ):
        raise ValueError("Penalty per day must be between 0 and 100 percent")
    if "scheduler.penalty_grace_days" in values and not (
        0 <= int(values["scheduler.penalty_grace_days"]) <= 365
    ):
        raise ValueError("Grace period must be between 0 and 365 days")
    if "scheduler.penalty_max_amount" in values and float(values["scheduler.penalty_max_amount"]) < 0:
        raise ValueError("Maximum penalty cannot be negative")

    if "meter.tenant_upload_from_day" in values or "meter.tenant_upload_to_day" in values:
        for key in ("meter.tenant_upload_from_day", "meter.tenant_upload_to_day"):
            if key in values and not (1 <= int(values[key]) <= 31):
                raise ValueError("Tenant upload window days must be between 1 and 31")
        merged = dict(current or {})
        merged.update(values)
        start = int(merged.get("meter.tenant_upload_from_day", DEFAULTS["meter.tenant_upload_from_day"]["value"]))
        end   = int(merged.get("meter.tenant_upload_to_day",   DEFAULTS["meter.tenant_upload_to_day"]["value"]))
        if start > end:
            # Left unchecked this silently produces a window no day satisfies,
            # locking every tenant out with nothing on screen to explain it.
            raise ValueError(
                f"Tenant upload window: the from day ({start}) cannot be after "
                f"the to day ({end}). For a window that crosses month end, turn "
                f"on 'Allow tenant reading upload every day' instead."
            )

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
