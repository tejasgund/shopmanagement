"""
scheduler/task_runner.py - shared plumbing for the per-task scripts.

Each task has its own script so they can be given their own crontab entries -
different times, different frequencies, one disabled without touching the
others. What they all need is identical, so it lives here rather than being
copied three ways:

    connect -> read settings -> make sure the occurrence exists -> execute -> record

Everything one of these scripts writes goes to that task's own log file. Run
rent generation and the penalty task in the same minute and you still get two
separate, complete accounts of what each one did.

Settings come from the DATABASE (the Scheduler settings screen), not from
scheduler.conf - that file only says how to reach the database and where to
log. One switch, one place, whichever way the task was started.

Execution goes through the same coordinator the master sweep uses, so a task
run from its own script and the same task run by the master behave identically
and land in the same ledger. The script decides WHICH task; it does not decide
what the task does or whether it is allowed to run.
"""

from datetime import datetime

from scheduler import db, errors
from scheduler import master as scheduler_master
from scheduler import service as svc
from scheduler import settings as scheduler_settings
from scheduler.logging_setup import get_logger


def run_one(task_name: str, date_str: str = None) -> int:
    """
    Run every outstanding occurrence of one task. Returns a process exit code.

    Exit codes are the ones in scheduler/errors.py, identical across every
    script here and run_scheduler.py, so one monitoring rule covers them all:
    0 ran, 1 the run itself failed, 2 skipped (switched off), 3 bad usage.
    """
    log = get_logger(task_name)

    # Parsed before connecting: a typo in a backfill date should cost nothing.
    if date_str:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            log.error("--date must be YYYY-MM-DD, got %r", date_str)
            return errors.EXIT_BAD_USAGE
    else:
        target = None

    try:
        with db.session_scope(log) as session:
            cfg = scheduler_settings.get_all(session, log)

            # Both switches, checked in the order an operator expects. Recorded
            # as SKIPPED by the coordinator rather than exited on quietly, so
            # the dashboard still shows the run was considered.
            if not svc.scheduler_enabled(cfg):
                log.info("Skipped: the master scheduler is OFF in Scheduler settings")
                scheduler_master.run_due_tasks(session, only_task=task_name)
                return errors.EXIT_SKIPPED
            if not svc.task_enabled(cfg, task_name):
                log.info("Skipped: this task is OFF in Scheduler settings")
                scheduler_master.run_due_tasks(session, only_task=task_name)
                return errors.EXIT_SKIPPED

            # Make sure there is something to run. A per-task script may be the
            # only thing scheduled on this box, so it cannot assume the
            # future-task checker has been round to register today's occurrence.
            run_for = target or svc.now_local().date()
            when = svc.occurrence_at(task_name, run_for)
            if svc.register(session, task_name, when, run_date=run_for):
                log.info("Registered the occurrence for %s", run_for)
            session.commit()

            summary = scheduler_master.run_due_tasks(session, only_task=task_name)

            for row in summary["skipped"]:
                log.info("%s SKIPPED - %s", row["run_date"], row["skip_reason"])
            for row in summary["failed"]:
                first = (row["error_message"] or "").splitlines()[0] if row["error_message"] else ""
                log.error("%s FAILED - %s", row["run_date"], first)

            log.info("Done: %s run, %s failed, %s skipped",
                     summary["tasks_run"], summary["tasks_failed"], summary["tasks_skipped"])

            # A failed TASK is already recorded and retryable from the
            # dashboard; exiting non-zero as well would have cron mail
            # duplicating it. Only an exception escaping this function is a
            # failed RUN.
            return errors.EXIT_OK

    except errors.SchedulerError as exc:
        # A problem with the scheduler itself - unreachable database, unusable
        # config. Nothing was attempted, so there is no ledger row to explain
        # it; the log is the only record and cron should hear about it.
        log.error("%s could not run | %s", task_name, errors.describe(exc))
        return errors.EXIT_RUN_FAILED
    except Exception as exc:          # noqa: BLE001
        errors.log_exception(log, f"{task_name} run failed", exc)
        return errors.EXIT_RUN_FAILED
