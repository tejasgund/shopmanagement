"""
tasks/future_task_checker.py - registers what is coming, and finds what was missed.

Contains no rent or penalty logic whatsoever: it only ever writes task rows.
It works entirely from scheduler.service.TASKS, so a task added to that
registry starts appearing in the upcoming list and the missed-run sweep with
no change here, in the API, or in the dashboard - there is no second list of
tasks anywhere to keep in step.

Two jobs, one pass:

  * FORWARD - write a PENDING row for every occurrence in the next
    `scheduler.lookahead_days`. Registering ahead is what makes a missed run
    detectable at all: the row already exists, so a run that never happens is
    a PENDING row with a past scheduled_for rather than an absence nobody can
    see.

  * BACKWARD - within `scheduler.backfill_days`, write rows for occurrences
    that were never registered, so an outage that spanned the registration
    window itself is still recovered.

The backward pass deliberately does NOT invent history for a task that has
never run. On a fresh install, or the day a new task is added, there is no
sense in which last month's runs were "missed" - manufacturing thirty failed
rows would bury the dashboard in noise on day one. Backfill therefore starts
no earlier than the first occurrence the task already knows about.
"""

from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from scheduler import service as svc
from scheduler.models import SchedulerTask


def run(db: Session, run_date: date, cfg: dict) -> dict:
    lookahead = int(cfg.get("scheduler.lookahead_days") or 0)
    backfill = int(cfg.get("scheduler.backfill_days") or 0)
    today = svc.now_local().date()

    registered_future = []
    registered_missed = []

    for task_name in svc.TASKS:
        # ── forward ──
        for offset in range(0, lookahead + 1):
            day = today + timedelta(days=offset)
            when = svc.occurrence_at(task_name, day)
            if svc.register(db, task_name, when, run_date=day):
                registered_future.append(f"{task_name}@{when.isoformat()}")

        # ── backward ──
        if backfill > 0:
            earliest_known = (
                db.query(func.min(SchedulerTask.scheduled_for))
                .filter(SchedulerTask.task_name == task_name)
                .scalar()
            )
            if earliest_known is not None:
                # Never before this task first existed - see the docstring.
                floor_day = max(earliest_known.date(), today - timedelta(days=backfill))
                day = floor_day
                while day < today:
                    when = svc.occurrence_at(task_name, day)
                    if svc.register(db, task_name, when, run_date=day):
                        registered_missed.append(f"{task_name}@{when.isoformat()}")
                    day += timedelta(days=1)

    db.commit()

    # Everything now overdue and unprocessed, for the run log and the dashboard.
    outstanding = [svc.to_dict(r) for r in svc.due_tasks(db)]
    missed = [t for t in outstanding if t["is_missed"]]
    stuck = [
        svc.to_dict(r)
        for r in db.query(SchedulerTask).filter(SchedulerTask.status == svc.RUNNING).all()
        if svc.is_stuck(r)
    ]

    return {
        "records_processed": len(registered_future) + len(registered_missed),
        "records_failed": 0,
        "future_registered": len(registered_future),
        "missed_registered": len(registered_missed),
        "missed_registered_detail": registered_missed[:50],
        "outstanding_now": len(outstanding),
        "missed_now": len(missed),
        "stuck_now": len(stuck),
        "known_tasks": sorted(svc.TASKS),
        "lookahead_days": lookahead,
        "backfill_days": backfill,
    }
