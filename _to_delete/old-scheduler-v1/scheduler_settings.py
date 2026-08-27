"""
scheduler/settings.py - the scheduler's switches, and the one app setting it reads.

The scheduler owns every `scheduler.*` key: this module is where they are
declared, defaulted, documented and validated. The application imports
SCHEDULER_DEFAULTS from here to render and save them on its Scheduler settings
screen, so there is one definition and the dependency runs app -> scheduler,
never the other way.

The values live in the shared `app_settings` table rather than in
scheduler.conf. That is deliberate: a switch in a file on the server and a
toggle in the UI would be two answers to the same question, and whichever the
operator checked first would be the wrong one. scheduler.conf answers only
"how do I reach the database"; everything else is read FROM the database once
connected.

No cache. Every scheduler run is a fresh, short-lived process that reads its
settings once at the start of the sweep - the API's cache exists to avoid
re-reading on every request, which is a problem this process does not have.
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from scheduler.models import AppSetting

# ══════════════════════════════════════════════════════════════════════════════
# OWNED BY THE SCHEDULER
#
# The cron entry only decides how OFTEN the scheduler wakes up. These decide
# whether it does anything when it does, and are read fresh on every run - so
# turning something off takes effect on the next tick, with nothing to restart
# and no crontab to edit.
# ══════════════════════════════════════════════════════════════════════════════

SCHEDULER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "scheduler.enabled": {
        "value": True, "type": "bool", "category": "Scheduler",
        "label": "Master scheduler",
        "help": "Off: cron still wakes the scheduler, but no task does any work - "
                "every due task is recorded as SKIPPED rather than silently "
                "ignored, so the dashboard shows exactly what did not run.",
    },
    "scheduler.rent_generation_enabled": {
        "value": True, "type": "bool", "category": "Scheduler",
        "label": "Automatic rent generation",
        "help": "Generate each tenant's monthly Rent bill on their configured "
                "bill day. Only runs when the master scheduler above is on.",
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
    "scheduler.penalty_max_amount": {
        "value": 0, "type": "float", "category": "Scheduler",
        "label": "Maximum penalty per bill",
        "help": "Upper limit on the accrued penalty for a single bill. 0 means "
                "no cap, and the penalty keeps growing until the bill is paid.",
    },
    "scheduler.backfill_days": {
        "value": 30, "type": "int", "category": "Scheduler",
        "label": "Look back for missed runs (days)",
        "help": "How far back the scheduler looks for runs that never happened - "
                "a server outage, a stopped cron. Anything found is registered "
                "and processed rather than lost. Keep it comfortably longer than "
                "the longest outage you would want recovered automatically.",
    },
    "scheduler.lookahead_days": {
        "value": 7, "type": "int", "category": "Scheduler",
        "label": "Register future runs (days ahead)",
        "help": "How far ahead the future-task checker writes expected runs, so "
                "the dashboard can show what is coming. Registering them in "
                "advance is also what makes a missed run detectable: the row "
                "already exists and simply never completed.",
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# OWNED BY THE APPLICATION, READ HERE
#
# Not exported to the app - it declares these itself. The fallback below is
# only what this process uses when the key has never been customised and the
# row therefore does not exist. Keep it equal to the app's own default; the
# parity test in the app's suite checks that it is.
# ══════════════════════════════════════════════════════════════════════════════

EXTERNAL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "bill.due_days": {
        "value": 30, "type": "int", "category": "Billing",
        "label": "Default bill due period (days)",
        "help": "Read by rent generation to set each new bill's due date. Owned "
                "by the application's Settings screen, not the scheduler's.",
    },
}

DEFAULTS: Dict[str, Dict[str, Any]] = {**SCHEDULER_DEFAULTS, **EXTERNAL_DEFAULTS}

SCHEDULER_PREFIX = "scheduler."


def is_scheduler_key(key: str) -> bool:
    return str(key).startswith(SCHEDULER_PREFIX)


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


def get_all(db: Session, logger=None) -> Dict[str, Any]:
    """
    Every setting this process cares about: defaults overlaid with whatever the
    admin has actually changed.

    A missing app_settings table, or an unreadable value, falls back to the
    default and says so in the log rather than failing the run. A scheduler
    that refuses to start because one setting is malformed is worse than one
    that runs with the documented default and tells you.
    """
    values = {key: spec["value"] for key, spec in DEFAULTS.items()}
    try:
        rows = db.query(AppSetting).filter(AppSetting.key.in_(list(DEFAULTS))).all()
    except Exception as exc:
        if logger:
            logger.warning("Could not read app_settings (%s); using defaults.", exc)
        return values

    for row in rows:
        try:
            values[row.key] = _coerce(row.value, DEFAULTS[row.key]["type"])
        except (TypeError, ValueError):
            if logger:
                logger.warning(
                    "Setting %s has an unusable stored value (%r); using default.",
                    row.key, row.value,
                )
    return values


def get(db: Session, key: str, logger=None) -> Any:
    """One resolved setting value. Unknown keys raise KeyError."""
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    return get_all(db, logger).get(key, DEFAULTS[key]["value"])


def describe() -> list:
    """
    The schema for the settings the scheduler owns, for the app's Scheduler
    settings screen: key, label, help, type, category and factory default.
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
        for key, spec in SCHEDULER_DEFAULTS.items()
    ]


def validate(values: Dict[str, Any]) -> Optional[str]:
    """
    Check a proposed set of scheduler settings. Returns the first problem as a
    sentence, or None when everything is acceptable.

    Lives here, not in the app, because these are the scheduler's rules: the
    bounds exist to stop a value that would make a RUN behave absurdly (a 500%
    daily penalty, a backfill that walks back two years every night), and the
    scheduler is what has to live with them.
    """
    if "scheduler.penalty_percent_per_day" in values:
        percent = float(values["scheduler.penalty_percent_per_day"])
        if not 0 <= percent <= 100:
            return "Penalty per day must be between 0 and 100 percent."

    if "scheduler.penalty_grace_days" in values:
        grace = int(values["scheduler.penalty_grace_days"])
        if not 0 <= grace <= 365:
            return "Grace period must be between 0 and 365 days."

    if "scheduler.penalty_max_amount" in values:
        if float(values["scheduler.penalty_max_amount"]) < 0:
            return "Maximum penalty cannot be negative."

    if "scheduler.backfill_days" in values:
        if not 0 <= int(values["scheduler.backfill_days"]) <= 400:
            return "Look back for missed runs must be between 0 and 400 days."

    if "scheduler.lookahead_days" in values:
        if not 0 <= int(values["scheduler.lookahead_days"]) <= 90:
            return "Register future runs must be between 0 and 90 days."

    return None
