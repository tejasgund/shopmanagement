#!/usr/bin/env python3
"""
scheduler/run_scheduler.py - the master cron entry point.

One sweep of everything that is due, then exit. Cron decides when; this
decides what.

    python -m scheduler.run_scheduler master                    # what cron calls
    python -m scheduler.run_scheduler master --task rent_generation
    python -m scheduler.run_scheduler --list

Writes coordination lines to logs/master.log - which tasks got a turn and how
each ended. What each task actually DID is in that task's own file, so a
failure in one is never buried in another's output.

Why this exists instead of the old in-app scheduler: the API runs under
`uvicorn --workers 2`, and every worker ran the FastAPI startup hook, so every
worker started its own APScheduler and the same nightly job fired twice at the
same second. The database lock made that harmless but it was work done twice
for no reason, and "how many times did the job run last night?" depended on
how many workers happened to be up. As a cron entry it runs exactly once,
where the rest of the box's scheduled work already lives, and it can be run by
hand or backfilled without restarting anything.

Exit codes, so cron mail and any monitoring can tell these apart:
    0  the sweep ran (including "ran and had nothing to do")
    1  the sweep itself failed - the log has the traceback
    2  reserved: a single task skipped because it is disabled (per-task scripts)
    3  bad usage (unknown job name)
"""

import argparse
import sys

from scheduler import _bootstrap  # noqa: F401  (sys.path + .env; must be first)

from scheduler import config, db, errors      # noqa: E402
from scheduler.logging_setup import get_logger  # noqa: E402

log = get_logger("master")


# ══════════════════════════════════════════════════════════════════════════════
# JOBS
# ══════════════════════════════════════════════════════════════════════════════

def job_master(args) -> int:
    """
    The master scheduler: coordinate, do not compute.

    Everything about WHICH tasks run and what they do lives in
    scheduler.master / scheduler.service / scheduler.tasks. This function only
    supplies a database session and turns the summary into a run log and an
    exit code.
    """
    from scheduler import master as scheduler_master

    log.info("Sweep starting | database %s", config.safe_database_url())

    with db.session_scope(log) as session:
        summary = scheduler_master.run_due_tasks(session, only_task=args.task)

    if not summary["scheduler_enabled"]:
        log.info("Scheduler is DISABLED in Scheduler settings - %s due task(s) "
                 "recorded as skipped", summary["tasks_skipped"])
        return errors.EXIT_OK

    for row in summary["executed"]:
        log.info("%s for %s COMPLETED (%s processed, %sms)",
                 row["task_name"], row["run_date"],
                 row["records_processed"], row["duration_ms"])
    for row in summary["skipped"]:
        log.info("%s for %s SKIPPED - %s",
                 row["task_name"], row["run_date"], row["skip_reason"])
    for row in summary["failed"]:
        first = (row["error_message"] or "").splitlines()[0] if row["error_message"] else ""
        log.error("%s for %s FAILED - %s (see logs/%s.log)",
                  row["task_name"], row["run_date"], first, row["task_name"])

    log.info("Sweep done: %s run, %s failed, %s skipped",
             summary["tasks_run"], summary["tasks_failed"], summary["tasks_skipped"])

    # A failed TASK is not a failed SWEEP: the other tasks still ran, and the
    # failure is already recorded and retryable from the dashboard. Exiting
    # non-zero here would make cron mail on something the dashboard already
    # owns. Only an exception escaping the sweep itself is a run failure.
    return errors.EXIT_OK


JOBS = {
    "master": {
        "run": job_master,
        "help": "Coordinate every due scheduler task. This is the one cron should call.",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_scheduler.py",
        description="Run one scheduled job and exit. Intended to be driven by cron.",
    )
    parser.add_argument("job", nargs="?", help="which job to run (see --list)")
    parser.add_argument("--task", help="master only: restrict the sweep to this one task")
    parser.add_argument("--list", action="store_true", help="list the available jobs and exit")
    args = parser.parse_args(argv)

    if args.list or not args.job:
        print("Available jobs:")
        for name, spec in JOBS.items():
            print(f"  {name:12s} {spec['help']}")
        return errors.EXIT_OK if args.list else errors.EXIT_BAD_USAGE

    if args.job not in JOBS:
        log.error("Unknown job %r. Known jobs: %s", args.job, ", ".join(JOBS))
        return errors.EXIT_BAD_USAGE

    # No enable checks here: the switches live in the database and are read by
    # the coordinator, so a run that is turned off is RECORDED as skipped
    # rather than exiting before anything notices it was due.
    try:
        return JOBS[args.job]["run"](args)
    except errors.SchedulerError as exc:
        log.error("%s could not run | %s", args.job, errors.describe(exc))
        return errors.EXIT_RUN_FAILED
    except Exception as exc:          # noqa: BLE001
        errors.log_exception(log, f"{args.job} failed", exc)
        return errors.EXIT_RUN_FAILED


if __name__ == "__main__":
    sys.exit(main())
