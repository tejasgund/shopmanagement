"""
scheduler/billing/penalty.py - the daily late-payment penalty on overdue bills.

Owned by the scheduler; the application imports `quote` and `penalty_settings`
to explain a figure to a tenant, so the explanation can never disagree with
the charge. Sits beside rent.py and follows the same shape: pure business
logic, no FastAPI, no scheduler concepts, driven by whatever session it is
handed. Contains no rent-generation logic, and rent.py contains none of this.

The penalty is always RECOMPUTED FROM SCRATCH for a bill, never incremented.
That single decision is what makes the task idempotent: running it twice in a
day, retrying a failed run, or processing a backfilled day out of order all
converge on the same number, because the answer is a function of (original
amount, due date, as-of date, settings) and nothing else. An incrementing
implementation would need every one of those cases handled by hand.

`Bill.amount` is never touched. The penalty accumulates in `penalty_amount`,
so "what was this bill for" and "what is owed now" stay separately answerable
however long a bill has been outstanding.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from scheduler import settings as scheduler_settings
from scheduler.logging_setup import get_logger
from scheduler.models import Bill
from scheduler.money import decimal_to_float, reconcile_bill

logger = get_logger("due_date_penalty")

# Penalty rows are themselves bills; charging a penalty on a penalty would
# compound a late fee into a debt spiral. Excluded by type, defensively, even
# though nothing currently creates bills of these types.
NON_PENALISABLE_TYPES = {"penalty", "late fee", "latefee"}


def penalty_settings(cfg: dict) -> dict:
    """The configured rules, normalised."""
    return {
        "enabled": bool(cfg.get("scheduler.penalty_enabled")),
        "percent_per_day": float(cfg.get("scheduler.penalty_percent_per_day") or 0),
        "grace_days": int(cfg.get("scheduler.penalty_grace_days") or 0),
        "max_amount": float(cfg.get("scheduler.penalty_max_amount") or 0),
    }


def quote(bill, cfg: dict, as_of: date) -> dict:
    """
    What the penalty on this bill is as of `as_of` - the whole calculation,
    with every intermediate figure the dashboards have to show.

    Pure: reads the bill and the settings, changes nothing. The task uses it to
    decide what to write; the API uses the same function to explain a figure to
    a tenant, so the explanation can never disagree with the charge.

    Takes any object with `amount`, `due_date` and `penalty_amount` - the
    scheduler's Bill or the application's, since both map the same row.
    """
    rules = penalty_settings(cfg)
    original = decimal_to_float(bill.amount)
    due = bill.due_date.date() if bill.due_date else None

    out = {
        "original_amount": round(original, 2),
        "due_date": bill.due_date,
        "days_overdue": 0,
        "grace_days": rules["grace_days"],
        "chargeable_days": 0,
        "penalty_percent_per_day": rules["percent_per_day"],
        "penalty_per_day": 0.0,
        "total_penalty": 0.0,
        "max_penalty": rules["max_amount"],
        "capped": False,
        "total_payable": round(original, 2),
    }

    if due is None or original <= 0:
        return out

    out["days_overdue"] = max(0, (as_of - due).days)

    # Grace is measured from the due date, so a 5-day grace on a bill due on
    # the 10th starts charging on the 16th - the 15th is still free.
    chargeable = max(0, (as_of - (due + timedelta(days=rules["grace_days"]))).days)
    per_day = round(original * rules["percent_per_day"] / 100.0, 2)
    total = round(per_day * chargeable, 2)

    if rules["max_amount"] > 0 and total > rules["max_amount"]:
        total = round(rules["max_amount"], 2)
        out["capped"] = True

    out["chargeable_days"] = chargeable
    out["penalty_per_day"] = per_day
    out["total_penalty"] = total
    out["total_payable"] = round(original + total, 2)
    return out


def eligible_bills(db: Session) -> list:
    """
    Bills that can accrue a penalty: overdue, not fully paid, not a penalty.

    Deliberately not filtered by date here - `quote` decides whether anything
    is actually chargeable, so a bill inside its grace period is still visited
    and simply quoted at zero. Keeping the two concerns apart means the grace
    rule lives in exactly one place.
    """
    rows = (
        db.query(Bill)
        .filter(Bill.status != "paid", Bill.due_date.isnot(None))
        .order_by(Bill.id.asc())
        .all()
    )
    return [b for b in rows if (b.bill_type or "").strip().lower() not in NON_PENALISABLE_TYPES]


def apply_penalties_for_date(db: Session, target_date: date,
                             cfg: Optional[dict] = None) -> dict:
    """
    Bring every overdue bill's penalty up to date as of `target_date`.

    Safe to run repeatedly - see the module docstring on recomputation. A bill
    whose penalty is already correct is counted as unchanged rather than
    rewritten, so a second run in the same day touches nothing.

    One malformed bill is counted in records_failed and logged with its
    traceback; the rest of the run continues. Failure isolation applies within
    a task, not just between tasks.
    """
    cfg = cfg if cfg is not None else scheduler_settings.get_all(db, logger)
    rules = penalty_settings(cfg)

    summary = {
        "date": target_date.isoformat(),
        "records_processed": 0,
        "records_failed": 0,
        "bills_examined": 0,
        "bills_charged": 0,
        "bills_unchanged": 0,
        "bills_cleared": 0,
        "total_penalty_added": 0.0,
        "percent_per_day": rules["percent_per_day"],
        "grace_days": rules["grace_days"],
        "max_penalty": rules["max_amount"],
    }

    for bill in eligible_bills(db):
        summary["bills_examined"] += 1
        try:
            calc = quote(bill, cfg, target_date)
            new_penalty = Decimal(str(calc["total_penalty"]))
            old_penalty = Decimal(str(decimal_to_float(bill.penalty_amount)))

            if new_penalty == old_penalty:
                summary["bills_unchanged"] += 1
                # Still record how far the calculation has been carried, so the
                # dashboard can tell "checked and nothing due" from "never
                # looked at".
                bill.penalty_charged_through = target_date
                continue

            delta = float(new_penalty - old_penalty)
            bill.penalty_amount = new_penalty
            bill.penalty_days = calc["chargeable_days"]
            bill.penalty_charged_through = target_date

            # The one place that decides what a bill owes; calling it here is
            # what makes the new penalty visible to every payment route.
            reconcile_bill(bill)

            if new_penalty > old_penalty:
                summary["bills_charged"] += 1
                summary["total_penalty_added"] += delta
            else:
                # Settings were relaxed (lower percentage, a cap introduced) -
                # the recomputation legitimately reduces what is owed.
                summary["bills_cleared"] += 1

            summary["records_processed"] += 1
        except Exception:
            # One malformed bill must not cost the rest their penalty run.
            summary["records_failed"] += 1
            logger.exception("Penalty calculation failed for bill %s", bill.id)

    summary["total_penalty_added"] = round(summary["total_penalty_added"], 2)
    db.commit()
    return summary
