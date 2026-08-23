"""
scheduler_master.py - the coordinator.

Its entire job, and the limit of it:

    check the master switch -> find due tasks -> run each one -> record

There is no rent logic and no penalty logic here, and there must never be.
Each task owns its own rules in its own module; this decides only WHICH of
them get a turn and makes sure the outcome is written down.

Failure isolation is the other reason it exists. Every task runs inside
scheduler_service.execute(), which never raises - a task that blows up becomes
a FAILED row and the loop moves to the next one. One broken task cannot stop
the rest of the night's work.
"""

from typing import Optional

from sqlalchemy.orm import Session

from log import get_logger
import scheduler_service as svc
import settings_service

logger = get_logger("app")


def run_due_tasks(db: Session, only_task: Optional[str] = None) -> dict:
    """
    One sweep. Returns a summary the caller can log or return over the API.

    Settings are read once per sweep, so a task cannot see a different
    configuration from the one the sweep started under.
    """
    cfg = settings_service.get_all(db)
    started = svc.now_local()

    summary = {
        "started_at": started,
        "scheduler_enabled": svc.scheduler_enabled(cfg),
        "executed": [],
        "skipped": [],
        "failed": [],
        "tasks_run": 0,
        "tasks_failed": 0,
        "tasks_skipped": 0,
    }

    # ── The master switch ──
    # Cron still fired and this still ran; what stops is the WORK. Every due
    # occurrence is closed off as SKIPPED with the reason recorded, so the
    # dashboard shows exactly what did not happen and why, instead of a
    # silently growing pile of pending rows.
    if not svc.scheduler_enabled(cfg):
        for row in svc.due_tasks(db):
            svc.skip(db, row, "master scheduler is disabled in Scheduler settings")
            summary["skipped"].append(svc.to_dict(row))
        summary["tasks_skipped"] = len(summary["skipped"])
        logger.info("Scheduler disabled; %s due task(s) marked skipped", summary["tasks_skipped"])
        return summary

    # An empty ledger cannot schedule itself - see ensure_bootstrapped.
    summary["bootstrapped"] = svc.ensure_bootstrapped(db)

    # The future-task checker runs FIRST when it is due, so the same sweep can
    # discover and then process a run that was missed during an outage rather
    # than waiting for the next night to notice it.
    due = svc.due_tasks(db)
    due.sort(key=lambda r: (r.task_name != "future_task_checker", r.scheduled_for, r.id))

    for row in due:
        if only_task and row.task_name != only_task:
            continue

        finished = svc.execute(db, row, cfg)
        record = svc.to_dict(finished)

        if finished.status == svc.COMPLETED:
            summary["executed"].append(record)
            summary["tasks_run"] += 1
        elif finished.status == svc.FAILED:
            summary["failed"].append(record)
            summary["tasks_failed"] += 1
        elif finished.status == svc.SKIPPED:
            summary["skipped"].append(record)
            summary["tasks_skipped"] += 1

        # A newly-run future-task checker may have just registered a missed
        # occurrence that is already due. Pick those up in this same sweep.
        if finished.task_name == "future_task_checker" and finished.status == svc.COMPLETED:
            known = {r.id for r in due}
            for extra in svc.due_tasks(db):
                if extra.id not in known:
                    due.append(extra)

    summary["finished_at"] = svc.now_local()
    return summary
