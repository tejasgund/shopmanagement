"""
scheduler/service.py - the scheduler's ledger and task registry.

Contains NO business logic. It knows which tasks exist, when each occurrence
was due, and what happened to it; it does not know what a rent bill or a
penalty is. That separation is the point: the master scheduler coordinates,
the ledger records, and each task owns its own rules.

The database is the source of truth for what was SUPPOSED to happen. Every
expected occurrence is written as a PENDING row before it is due, so a run
that never happened is not an absence to be inferred - it is a row sitting in
the table with a scheduled_for in the past, which is what makes "nothing is
silently missed" enforceable rather than aspirational.

Adding a task means adding one entry to TASKS below. It then appears in the
dashboard, the future list and the missed-run sweep automatically - there is
no second list anywhere, in the frontend or otherwise, to keep in step.
"""

import json
import traceback
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_config import APP_TIMEZONE
from create_tables import SchedulerTask
from log import get_logger

logger = get_logger("app")

PENDING, RUNNING, COMPLETED, FAILED, SKIPPED = (
    "PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED",
)

# A task is considered late once it is this far past its scheduled time. Short
# enough to surface a genuinely stuck cron quickly, long enough that a run in
# progress is not flagged as missed while it works.
MISSED_AFTER = timedelta(minutes=30)

# A RUNNING row older than this was almost certainly abandoned - the process
# was killed, the container restarted - rather than still working. Shown as
# stuck so it can be retried instead of blocking its next occurrence forever.
STUCK_AFTER = timedelta(hours=2)


# ══════════════════════════════════════════════════════════════════════════════
# REGISTRY
#
# `runner` is an import path resolved only when the task actually runs, so this
# module never imports the business logic and cannot end up in an import cycle
# with it.
# ══════════════════════════════════════════════════════════════════════════════

TASKS = {
    "future_task_checker": {
        "label": "Future Task Checker",
        "description": "Registers upcoming runs and detects ones that never happened.",
        "enable_setting": None,          # structural: runs whenever the scheduler is on
        "run_at": time(1, 55),
        "runner": "scheduler.tasks.future_task_checker:run",
    },
    "rent_generation": {
        "label": "Rent Generation",
        "description": "Creates each tenant's monthly Rent bill on their bill day.",
        "enable_setting": "scheduler.rent_generation_enabled",
        "run_at": time(2, 0),
        "runner": "scheduler.tasks.rent_generation:run",
    },
    "due_date_penalty": {
        "label": "Due Date Penalty",
        "description": "Applies the daily late fee to overdue unpaid bills.",
        "enable_setting": "scheduler.penalty_enabled",
        "run_at": time(2, 5),
        "runner": "scheduler.tasks.due_date_penalty:run",
    },
}


def now_local() -> datetime:
    """Naive local time, matching how the rest of the app stores timestamps."""
    return datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)


def occurrence_at(task_name: str, day: date) -> datetime:
    """The datetime this task was due on `day`."""
    return datetime.combine(day, TASKS[task_name]["run_at"])


def task_enabled(cfg: dict, task_name: str) -> bool:
    """Is this task switched on? Structural tasks have no switch of their own."""
    key = TASKS[task_name].get("enable_setting")
    if key is None:
        return True
    return bool(cfg.get(key))


def scheduler_enabled(cfg: dict) -> bool:
    return bool(cfg.get("scheduler.enabled"))


# ══════════════════════════════════════════════════════════════════════════════
# REGISTERING OCCURRENCES
# ══════════════════════════════════════════════════════════════════════════════

def register(db: Session, task_name: str, scheduled_for: datetime,
             run_date: Optional[date] = None) -> Optional[SchedulerTask]:
    """
    Ensure a PENDING row exists for this occurrence. Returns it, or None if it
    already existed.

    Duplicate protection is the unique index on (task_name, scheduled_for), not
    a check-then-insert: two cron ticks racing, or a backfill overlapping a
    live run, would both pass a check and both insert. Here the second one
    loses at the database and is simply told the row is already there.
    """
    existing = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.task_name == task_name,
                SchedulerTask.scheduled_for == scheduled_for)
        .first()
    )
    if existing:
        return None

    row = SchedulerTask(
        task_name=task_name,
        scheduled_for=scheduled_for,
        run_date=run_date or scheduled_for.date(),
        status=PENDING,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Lost the race - the other writer's row is the one that counts.
        db.rollback()
        return None
    return row


BOOTSTRAP_TASK = "future_task_checker"


def ensure_bootstrapped(db: Session) -> bool:
    """
    Make sure every known task has future coverage, scheduling the checker if not.

    Two situations need this, and they are the same situation:

      * A brand-new install. Everything is registered by the future-task
        checker, which only runs because a row says it is due - so an empty
        ledger would stay empty forever, the scheduler dutifully finding
        nothing to do every night.

      * A task added to TASKS later. Its first occurrence would otherwise not
        be written until the next nightly checker run, so a task deployed at
        10am would be invisible on the dashboard until tomorrow.

    Both are "some task in the registry has nothing scheduled ahead of it",
    which is exactly what is tested here. Registering the checker to run now
    resolves both, and the guard below stops it queuing a fresh checker on
    every sweep while one is already waiting.
    """
    now = now_local()

    uncovered = [
        name for name in TASKS
        if not db.query(SchedulerTask)
                 .filter(SchedulerTask.task_name == name,
                         SchedulerTask.scheduled_for >= now)
                 .first()
    ]
    if not uncovered:
        return False

    already_queued = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.task_name == BOOTSTRAP_TASK,
                SchedulerTask.status == PENDING,
                SchedulerTask.scheduled_for <= now)
        .first()
    )
    if already_queued:
        return False

    register(db, BOOTSTRAP_TASK, now, run_date=now.date())
    db.commit()
    logger.info(
        "Scheduler: %s have no future runs registered - queueing %s now",
        ", ".join(sorted(uncovered)), BOOTSTRAP_TASK,
    )
    return True


def due_tasks(db: Session, at: Optional[datetime] = None) -> list:
    """
    Everything that should have run by `at` and has not finished.

    Oldest first, so a backlog after an outage is worked through in the order
    it was meant to happen rather than newest-first.
    """
    at = at or now_local()
    return (
        db.query(SchedulerTask)
        .filter(SchedulerTask.status == PENDING, SchedulerTask.scheduled_for <= at)
        .order_by(SchedulerTask.scheduled_for.asc(), SchedulerTask.id.asc())
        .all()
    )


# ══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

def claim(db: Session, row: SchedulerTask) -> bool:
    """
    Move a row PENDING -> RUNNING, returning False if someone else got there.

    The status is re-checked inside an UPDATE ... WHERE status = 'PENDING'
    rather than trusted from the object we loaded, so two schedulers starting
    at the same second cannot both believe they own the same occurrence.
    """
    updated = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.id == row.id, SchedulerTask.status == PENDING)
        .update(
            {
                "status": RUNNING,
                "started_at": now_local(),
                "attempts": SchedulerTask.attempts + 1,
                "finished_at": None,
                "error_message": None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    db.refresh(row)
    return bool(updated)


def _finish(db: Session, row: SchedulerTask, status: str, **fields) -> SchedulerTask:
    finished = now_local()
    row.status = status
    row.finished_at = finished
    if row.started_at:
        row.duration_ms = int((finished - row.started_at).total_seconds() * 1000)
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def complete(db: Session, row: SchedulerTask, result: dict) -> SchedulerTask:
    """Mark a run successful, keeping whatever the task wants to show later."""
    return _finish(
        db, row, COMPLETED,
        records_processed=int(result.get("records_processed", 0) or 0),
        records_failed=int(result.get("records_failed", 0) or 0),
        result_json=json.dumps(result, default=str),
        error_message=None,
    )


def fail(db: Session, row: SchedulerTask, exc: BaseException) -> SchedulerTask:
    """
    Mark a run failed, keeping the traceback.

    The row stays FAILED rather than reverting to PENDING: an automatic retry
    on the next tick would hide a persistent fault behind an ever-growing
    attempt count. Retrying is a decision someone takes from the dashboard.
    """
    db.rollback()
    detail = f"{type(exc).__name__}: {exc}".strip()
    logger.error("Scheduler task %s failed: %s", row.task_name, detail)
    return _finish(
        db, row, FAILED,
        error_message=(detail + "\n\n" + traceback.format_exc())[:4000],
    )


def skip(db: Session, row: SchedulerTask, reason: str) -> SchedulerTask:
    """
    Record that an occurrence deliberately did no work.

    Skipped rather than deleted or left pending: "the scheduler was off that
    night" is an answer, and it is a different answer from "it never ran and
    nobody knows why".
    """
    return _finish(db, row, SKIPPED, skip_reason=reason[:255])


def retry(db: Session, row: SchedulerTask) -> SchedulerTask:
    """Put a finished run back in the queue. attempts is deliberately kept."""
    row.status = PENDING
    row.started_at = None
    row.finished_at = None
    row.duration_ms = None
    row.error_message = None
    row.skip_reason = None
    db.commit()
    db.refresh(row)
    return row


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def _resolve(runner: str):
    module_path, _, func_name = runner.partition(":")
    module = __import__(module_path, fromlist=[func_name])
    return getattr(module, func_name)


def execute(db: Session, row: SchedulerTask, cfg: dict) -> SchedulerTask:
    """
    Run one occurrence and record the outcome. Never raises.

    Swallowing the exception here is what gives the master scheduler its
    failure isolation: one task blowing up becomes a FAILED row, and the next
    task still gets its turn.
    """
    if row.task_name not in TASKS:
        # A task removed from the code but still in the table. Not a failure -
        # there is simply nothing to run - but it must not sit PENDING forever.
        return skip(db, row, "task no longer exists in this build")

    if not task_enabled(cfg, row.task_name):
        return skip(db, row, "task is disabled in Scheduler settings")

    if not claim(db, row):
        return row      # another scheduler owns it

    try:
        runner = _resolve(TASKS[row.task_name]["runner"])
        result = runner(db, row.run_date, cfg) or {}
        return complete(db, row, result)
    except Exception as exc:          # noqa: BLE001 - isolation is the point
        return fail(db, row, exc)


# ══════════════════════════════════════════════════════════════════════════════
# VIEWS  (read-only; shared by the dashboard API and the runner's own logging)
# ══════════════════════════════════════════════════════════════════════════════

def is_missed(row: SchedulerTask, at: Optional[datetime] = None) -> bool:
    at = at or now_local()
    return row.status == PENDING and row.scheduled_for < (at - MISSED_AFTER)


def is_stuck(row: SchedulerTask, at: Optional[datetime] = None) -> bool:
    at = at or now_local()
    return row.status == RUNNING and bool(row.started_at) and row.started_at < (at - STUCK_AFTER)


def to_dict(row: SchedulerTask, at: Optional[datetime] = None) -> dict:
    at = at or now_local()
    spec = TASKS.get(row.task_name, {})
    running_for = None
    if row.status == RUNNING and row.started_at:
        running_for = int((at - row.started_at).total_seconds())

    result = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except ValueError:
            result = None

    return {
        "id": row.id,
        "task_name": row.task_name,
        "label": spec.get("label", row.task_name),
        "description": spec.get("description"),
        "scheduled_for": row.scheduled_for,
        "run_date": row.run_date,
        "status": row.status,
        "attempts": row.attempts,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "duration_ms": row.duration_ms,
        "running_for_seconds": running_for,
        "records_processed": row.records_processed,
        "records_failed": row.records_failed,
        "error_message": row.error_message,
        "skip_reason": row.skip_reason,
        "result": result,
        "is_missed": is_missed(row, at),
        "is_stuck": is_stuck(row, at),
    }
