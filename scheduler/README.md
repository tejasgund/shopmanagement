# scheduler/

Scheduled jobs for the Tenant Management System, run by **cron** — not by the
application.

## Why it moved out of the app

The API runs under `uvicorn --workers 2`, and the old scheduler was started
from the FastAPI startup hook. Every worker ran that hook, so every worker
started its own APScheduler and the same nightly job fired twice at the same
second. A database lock made that harmless, but the work was still done twice
and "how many times did it run last night?" depended on how many workers
happened to be up. As a cron entry it runs exactly once, lives where the rest
of the box's scheduled work lives, and can be run by hand or backfilled
without restarting the API.

## What is in here

| File | Purpose |
| --- | --- |
| `run_scheduler.py` | The entry point cron calls. Runs one job and exits. |
| `db_config.py` | How this process reaches the database — its own small engine, separate from the API's pool. |
| `scheduler.conf` | Whether each job is allowed to run, its timezone, and the database connection. |
| `crontab.example` | Ready-to-paste cron entries. |

The job **logic** is deliberately *not* here. `rent_billing.py` sits in the
project root and is imported by both this runner and the admin's manual
`POST /api/bills/generate-rent`, so the nightly run and the button in the UI
execute the same code and cannot drift apart.

## Usage

```bash
cd /path/to/shopmanagement

python -m scheduler.run_scheduler --list                     # what can run
python -m scheduler.run_scheduler rent-bills                 # today
python -m scheduler.run_scheduler rent-bills --date 2026-08-01   # backfill
```

Run it as a module (`-m`) from the project root. That keeps the project root
first on `sys.path`, so this folder's `db_config.py` cannot shadow the
application's one of the same name.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Ran — including "ran and had nothing to do" |
| 1 | Failed; the traceback is in the run log |
| 2 | Skipped because it is disabled in `scheduler.conf` |
| 3 | Bad usage (unknown job, unparseable `--date`) |

Alert on anything non-zero except 2.

## Setup checklist

1. Fill in `[database]` in `scheduler.conf`, or make sure `DATABASE_URL` /
   `DB_*` are in the environment cron will have. **Cron does not inherit your
   shell's environment** — this is the single most common reason a job that
   works by hand fails at 2am.
2. `chmod 600 scheduler.conf` if you put a password in it.
3. Copy the entry from `crontab.example` into `crontab -e`, fixing the project
   path and the interpreter path.
4. Run it once by hand first and confirm the log looks right.

## Safety properties

* **Idempotent** — a tenant/shop already billed for that month is skipped, so
  running twice creates nothing twice.
* **Serialised** — a MySQL named lock means an overlapping cron run, or the
  admin pressing the manual button mid-run, cannot double-bill. The loser logs
  that it skipped and exits 0.
* **Timezone-explicit** — `timezone` in `scheduler.conf` decides *which day*
  the run is for, independently of the system clock cron fires on.
