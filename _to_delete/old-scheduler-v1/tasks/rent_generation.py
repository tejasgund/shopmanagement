"""
tasks/rent_generation.py - the Rent Generation scheduler task.

Deliberately thin. The rules live in scheduler/billing/rent.py, which the
admin's manual POST /api/bills/generate-rent also calls, so the nightly run and
the button cannot drift apart. No penalty logic here, and none of this in the
penalty task.

Duplicate protection is the billing module's own: a user/shop that already has
a Rent bill for that month is skipped. Re-running this task for the same day,
including a retry or a backfill of a day the server was down, therefore
creates nothing twice.
"""

from datetime import date

from sqlalchemy.orm import Session

from scheduler.billing import rent


def run(db: Session, run_date: date, cfg: dict) -> dict:
    summary = rent.generate_rent_bills_for_date_locked(db, run_date)

    created = summary.get("created", []) or []
    return {
        "records_processed": len(created),
        "records_failed": 0,
        "bills_created": created,
        "users_matched": summary.get("users_matched", 0),
        "skipped_existing": summary.get("skipped_existing", 0),
        "skipped_zero_rent": summary.get("skipped_zero_rent", 0),
        "skipped_no_shops": summary.get("skipped_no_shops", 0),
        # True when another run held the generation lock. Not a failure: that
        # run covers the same date.
        "skipped_locked": bool(summary.get("skipped_locked")),
    }
