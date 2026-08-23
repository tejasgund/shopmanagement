"""
scheduler/task_runner.py - shared plumbing for the per-task scripts.

Each task has its own script so they can be given their own crontab entries -
different times, different frequencies, one disabled without touching the
others. What they all need is identical, so it lives here rather than being
copied four ways:

    connect -> read settings -> make sure the occurrence exists -> execute -> record

Execution goes through the same coordinator the master sweep uses, so a task
run from its own script and the same task run by the master behave
identically and land in the same ledger. The script decides WHICH task; it
does not decide what the task does or whether it is allowed to run.
"""

import sys
import traceback
from datetime import datetime

from scheduler import db_config as scheduler_db


def run_one(task_name: str, log, date_str: str = None) -> int:
    """
    Run every outstanding occurrence of one task. Returns a process exit code.

    Exit codes match run_scheduler.py so all the scripts can be monitored the
    same way: 0 ran, 1 failed, 2 skipped (disabled), 3 bad usage.
    """
    import scheduler_master
    import scheduler_service as svc
    import settings_service

    engine, Session = scheduler_db.make_session_factory()
    db = Session()
    try:
        cfg = settings_service.get_all(db)

        # Both switches, checked in the order an operator expects. Recorded as
        # SKIPPED by the coordinator rather than exited on quietly, so the
        # dashboard still shows the run was considered.
        if not svc.scheduler_enabled(cfg):
            log(f"{task_name} | skipped: master scheduler is OFF in Scheduler settings")
            scheduler_master.run_due_tasks(db, only_task=task_name)
            return 2
        if not svc.task_enabled(cfg, task_name):
            log(f"{task_name} | skipped: this task is OFF in Scheduler settings")
            scheduler_master.run_due_tasks(db, only_task=task_name)
            return 2

        # Make sure there is something to run. A per-task script may be the
        # only thing scheduled on this box, so it cannot assume the future-task
        # checker has been round to register today's occurrence.
        if date_str:
            try:
                target = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                log(f"ERROR | --date must be YYYY-MM-DD, got {date_str!r}")
                return 3
        else:
            target = svc.now_local().date()

        when = svc.occurrence_at(task_name, target)
        if svc.register(db, task_name, when, run_date=target):
            log(f"{task_name} | registered the occurrence for {target}")
        db.commit()

        summary = scheduler_master.run_due_tasks(db, only_task=task_name)

        for row in summary["executed"]:
            log(f"{task_name} | {row['run_date']} COMPLETED "
                f"({row['records_processed']} processed, {row['duration_ms']}ms)")
        for row in summary["skipped"]:
            log(f"{task_name} | {row['run_date']} SKIPPED - {row['skip_reason']}")
        for row in summary["failed"]:
            first = (row["error_message"] or "").splitlines()[0] if row["error_message"] else ""
            log(f"{task_name} | {row['run_date']} FAILED - {first}")

        log(f"{task_name} | done: {summary['tasks_run']} run, "
            f"{summary['tasks_failed']} failed, {summary['tasks_skipped']} skipped")

        # A failed task is already recorded and retryable from the dashboard;
        # exiting non-zero as well would have cron mail duplicating it. Only an
        # exception escaping this function is a failed RUN.
        return 0
    except Exception:
        log(f"ERROR | {task_name} run failed:")
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return 1
    finally:
        db.close()
        engine.dispose()
