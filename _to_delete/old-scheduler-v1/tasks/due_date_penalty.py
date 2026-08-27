"""
tasks/due_date_penalty.py - the Due Date Penalty scheduler task.

Thin wrapper over scheduler/billing/penalty.py, which owns the rules. No
rent-generation logic here.

`run_date` matters: when this runs as a backfill for a day the server was
down, the penalty is calculated as of THAT day, not today, so recovering a
missed run cannot over-charge a tenant for days the system was not watching.
"""

from datetime import date

from sqlalchemy.orm import Session

from scheduler.billing import penalty


def run(db: Session, run_date: date, cfg: dict) -> dict:
    return penalty.apply_penalties_for_date(db, run_date, cfg)
