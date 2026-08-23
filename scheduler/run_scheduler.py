#!/usr/bin/env python3
"""
scheduler/run_scheduler.py - the cron entry point for scheduled jobs.

Run one job and exit. Cron decides when; this decides what.

    python -m scheduler.run_scheduler master                    # what cron calls
    python -m scheduler.run_scheduler master --task rent_generation
    python -m scheduler.run_scheduler rent-bills --date 2026-08-01   # direct backfill
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
    2  the job was skipped because it is disabled in scheduler.conf
    3  bad usage (unknown job name, unparseable date)
"""

import argparse
import os
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

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

# Reached by PACKAGE path, not as bare `config`/`db_config`. The project root
# is first on sys.path above precisely so that a bare `import db_config` from
# inside the application (create_tables.py does exactly that) still finds the
# APPLICATION's db_config.py rather than this folder's same-named one.
from scheduler import config as scheduler_conf   # noqa: E402
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

def job_rent_bills(args, conf) -> int:
    """Generate the monthly Rent bills due on a given day."""
    import rent_billing

    tz = conf["rent_bill_generation"]["timezone"]
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            _log(f"ERROR | --date must be YYYY-MM-DD, got {args.date!r}")
            return 3
    else:
        target_date = datetime.now(ZoneInfo(tz)).date()

    _log(f"rent-bills | target date {target_date} ({tz})")
    _log(f"rent-bills | database {scheduler_db.safe_url_for_logging()}")

    engine, Session = scheduler_db.make_session_factory()
    db = Session()
    try:
        summary = rent_billing.generate_rent_bills_for_date_locked(db, target_date)
    finally:
        db.close()
        engine.dispose()

    if summary.get("skipped_locked"):
        # Another run - the manual admin button, or an overlapping cron - held
        # the lock. Its run covers the same date, so this is a normal outcome
        # and not a failure.
        _log("rent-bills | skipped: another run already held the generation lock")
        return 0

    _log(
        "rent-bills | done: {created} created, matched {matched} tenant(s), "
        "skipped {existing} already-billed / {zero} zero-rent / {noshop} without shops".format(
            created=len(summary.get("created", [])),
            matched=summary.get("users_matched", 0),
            existing=summary.get("skipped_existing", 0),
            zero=summary.get("skipped_zero_rent", 0),
            noshop=summary.get("skipped_no_shops", 0),
        )
    )
    if summary.get("created"):
        _log(f"rent-bills | new bill ids: {summary['created']}")
    return 0


def job_master(args, conf) -> int:
    """
    The master scheduler: coordinate, do not compute.

    Everything about WHICH tasks run and what they do lives in
    scheduler_master / scheduler_service / tasks/. This function only supplies
    a database session and turns the summary into a run log and an exit code.
    """
    import scheduler_master

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
        "conf_section": "rent_bill_generation",
        "help": "Coordinate every due scheduler task. This is the one cron should call.",
    },
    "rent-bills": {
        "run": job_rent_bills,
        "conf_section": "rent_bill_generation",
        "help": "Generate monthly Rent bills for tenants due on the target date.",
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

    conf = scheduler_conf.load()
    spec = JOBS[args.job]

    # Two switches, checked in the order an operator would expect: the master
    # one, then the job's own.
    if not conf["scheduler_enabled"]:
        _log(f"{args.job} | skipped: [scheduler] enabled = false in scheduler.conf")
        return 2
    if not conf[spec["conf_section"]]["enabled"]:
        _log(f"{args.job} | skipped: [{spec['conf_section']}] enabled = false in scheduler.conf")
        return 2

    try:
        return spec["run"](args, conf)
    except Exception:
        # Cron discards a traceback that only goes to stderr on some setups,
        # so write it where the run log will keep it.
        _log(f"ERROR | {args.job} failed:")
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
