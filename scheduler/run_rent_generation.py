#!/usr/bin/env python3
"""
scheduler/run_rent_generation.py - run ONLY the Rent Generation task.

Its own script so it can have its own crontab entry, independent of the other
tasks. Everything it does - connect, read the settings, make sure the
occurrence exists, execute, record the result - is shared with the other
per-task scripts in task_runner.py, and execution goes through the same
coordinator the master sweep uses. Running this and letting the master run it
are therefore the same thing; only the schedule differs.

    python -m scheduler.run_rent_generation
    python -m scheduler.run_rent_generation --date 2026-08-01     # backfill one day

Exit codes: 0 ran, 1 failed, 2 skipped (disabled in settings), 3 bad usage.
"""

import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:
    pass

from scheduler.task_runner import run_one      # noqa: E402

TASK_NAME = "rent_generation"


def _log(message: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_rent_generation.py",
        description="Run the Rent Generation scheduler task and exit.",
    )
    parser.add_argument("--date", help="YYYY-MM-DD to run for; defaults to today")
    args = parser.parse_args(argv)
    return run_one(TASK_NAME, _log, args.date)


if __name__ == "__main__":
    sys.exit(main())
