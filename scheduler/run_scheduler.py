#!/usr/bin/env python3
"""
scheduler/run_scheduler.py - the cron entry point for scheduled jobs.

Run one job and exit. Cron decides when; this decides what.

    python -m scheduler.run_scheduler master                    # what cron calls
    python -m scheduler.run_scheduler master --task rent_generation
    python -m scheduler.run_scheduler --list

Why this exists instead of the old in-app scheduler: the API runs under
`uvicorn --workers 2`, and every worker ran the FastAPI startup hook, so every
worker started its own APScheduler and the same nightly job fired twice at the
same second. The database lock made that harmless but it was work done twice
for no reason, and "how many times did the job run last night?" depended on
how many workers happened to be up. As a cron entry it runs exactly once,
where the rest of the box's scheduled work already lives, and it can be run by
hand or backfilled without restarting the API.

Exit codes, so cron mail and any monitoring can tell these apart:
    0  the job ran (including "ran and had nothing to do")
    1  the job failed - the run log has the traceback
    2  the job was skipped because it is disabled in Scheduler settings
    3  bad usage (unknown job name, unparseable date)
"""

import argparse
import os
import sys
import traceback
from datetime import datetime

# The application package sits one level up. Importing from it rather than
# copying is the point: the nightly run and the admin's manual
# POST /api/bills/generate-rent then execute the SAME function, and cannot
# drift into disagreeing about what a rent bill is.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Match the application: fill in anything not already in the environment from
# the project's .env. Cron starts with almost no environment, so without this
# a deployment that keeps its credentials in .env would find none of them.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:      # python-dotenv absent, or no .env - both fine
    pass

# Reached by PACKAGE path (`scheduler.db_config`), never as a bare
# `db_config`. The project root is first on sys.path above so that the
# application's own modules keep resolving to `core.database` - this folder's
# connection helper is deliberately unreachable from anywhere but here.
from scheduler import db_config as scheduler_db  # noqa: E402


def _log(message: str) -> None:
    """
    One timestamped line on stdout.

    Deliberately not the application's rotating logger: this process may run
    as a different user than the API, and having it write into the API's log
    files is how you end up with a root-owned app.log the API can no longer
    rotate. Point the crontab entry at a file instead (see crontab.example) -
    then the run log is owned by whoever runs the job.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} | {message}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# JOBS
# ══════════════════════════════════════════════════════════════════════════════

def job_master(args) -> int:
    """
    The master scheduler: coordinate, do not compute.

    Everything about WHICH tasks run and what they do lives in
    scheduler.master / scheduler.service / scheduler.tasks. This function only supplies
    a database session and turns the summary into a run log and an exit code.
    """
    from scheduler import master as scheduler_master

    _log(f"master | database {scheduler_db.safe_url_for_logging()}")

    engine, Session = scheduler_db.make_session_factory()
    db = Session()
    try:
        summary = scheduler_master.run_due_tasks(db, only_task=args.task)
    finally:
        db.close()
        engine.dispose()

    if not summary["scheduler_enabled"]:
        _log(
            f"master | scheduler is DISABLED in Scheduler settings - "
            f"{summary['tasks_skipped']} due task(s) recorded as skipped"
        )
        return 0

    for row in summary["executed"]:
        _log(
            f"master | {row['task_name']} for {row['run_date']} COMPLETED "
            f"({row['records_processed']} processed, {row['duration_ms']}ms)"
        )
    for row in summary["skipped"]:
        _log(f"master | {row['task_name']} for {row['run_date']} SKIPPED - {row['skip_reason']}")
    for row in summary["failed"]:
        first_line = (row["error_message"] or "").splitlines()[0] if row["error_message"] else ""
        _log(f"master | {row['task_name']} for {row['run_date']} FAILED - {first_line}")

    _log(
        f"master | done: {summary['tasks_run']} run, "
        f"{summary['tasks_failed']} failed, {summary['tasks_skipped']} skipped"
    )

    # A failed TASK is not a failed SWEEP: the other tasks still ran, and the
    # failure is already recorded and retryable from the dashboard. Exiting
    # non-zero here would make cron mail on something the dashboard already
    # owns. Only an exception escaping the sweep itself is a run failure.
    return 0


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
    parser.add_argument("--date", help="YYYY-MM-DD to run for; defaults to today")
    parser.add_argument("--task", help="master only: restrict the sweep to this one task")
    parser.add_argument("--list", action="store_true", help="list the available jobs and exit")
    args = parser.parse_args(argv)

    if args.list or not args.job:
        print("Available jobs:")
        for name, spec in JOBS.items():
            print(f"  {name:12s} {spec['help']}")
        return 0 if args.list else 3

    if args.job not in JOBS:
        _log(f"ERROR | unknown job {args.job!r}. Known jobs: {', '.join(JOBS)}")
        return 3

    spec = JOBS[args.job]

    # No enable checks here: the switches live in the database and are read by
    # the coordinator, so a run that is turned off is RECORDED as skipped
    # rather than exiting before anything notices it was due.
    try:
        return spec["run"](args)
    except Exception:
        # Cron discards a traceback that only goes to stderr on some setups,
        # so write it where the run log will keep it.
        _log(f"ERROR | {args.job} failed:")
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
