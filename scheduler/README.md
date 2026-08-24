# scheduler/

Every scheduled job for the Tenant Management System, run by **cron**. Self
contained: all the scheduling machinery lives in this folder and nothing in it
is mixed into the application's own files.

## What is in here

| File | Purpose |
| --- | --- |
| `scheduler.conf` | **The one config file.** How to reach the database — nothing else. |
| `db_config.py` | Builds this process's engine from that file. |
| `service.py` | The task ledger: registry, statuses, due/missed queries. No business logic. |
| `master.py` | The coordinator. Finds due tasks, runs each in isolation, records results. |
| `tasks/` | One module per task. Thin — the rules live in the app (see below). |
| `run_scheduler.py` | `master` job: one entry that sweeps everything. |
| `run_rent_generation.py` | Rent Generation, on its own. |
| `run_due_date_penalty.py` | Due Date Penalty, on its own. |
| `run_future_task_checker.py` | Future Task Checker, on its own. |
| `task_runner.py` | Shared plumbing behind the three per-task scripts. |

## Two things deliberately NOT in here

**The billing rules.** `services/rent_billing.py` and `services/penalty_billing.py` stay in the
application, because the admin's manual "generate rent bills" button calls them
too. Copying them into this folder would give the 2am run and the button two
implementations that quietly drift apart — the exact failure this project has
already been bitten by. `tasks/` therefore imports them; the scheduling
machinery is separate, the domain rules are shared.

**The on/off switches.** They live in the database, edited in the Scheduler
app's Settings tab, so they can be changed without shell access and the
dashboard shows the same values the scripts obey. A file on the server saying
`enabled = false` and a toggle in the UI would be two answers to one question.

The dependency runs one way only: the application reads this package to render
the monitoring dashboard; nothing here imports `routers/` or `app.py`. The
folder stays independently runnable.

## Usage

```bash
cd /path/to/shopmanagement

# Option A — one entry, everything due:
python -m scheduler.run_scheduler master

# Option B — schedule each task separately:
python -m scheduler.run_future_task_checker
python -m scheduler.run_rent_generation
python -m scheduler.run_due_date_penalty

# Backfill a specific day (any of them):
python -m scheduler.run_rent_generation --date 2026-08-01
```

Run as a module (`-m`) from the project root. That keeps the project root first
on `sys.path`, so the application's own packages (`core/`, `models/`,
`services/`) resolve normally while this folder's `db_config.py` stays reachable
only as `scheduler.db_config`.

Pick Option A **or** Option B, not both — every task is idempotent so nothing
breaks, but two things picking up the same occurrence makes the run log
confusing. If you split them up, run the future-task checker first: it is what
registers upcoming runs and finds ones that were missed.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Ran — including "ran and had nothing to do" |
| 1 | The run itself failed; traceback in the log |
| 2 | Skipped because a switch is off in Scheduler settings |
| 3 | Bad usage (unknown job, unparseable `--date`) |

Alert on anything non-zero except 2. Note a **failed task** still exits 0: it is
recorded in the ledger and retryable from the dashboard, which is where a task
failure belongs rather than in cron mail. Only a failure of the sweep itself
exits 1.

## Setup checklist

1. Fill in `[database]` in `scheduler.conf`, or make sure `DATABASE_URL` /
   `DB_*` are in the environment cron will have. **Cron does not inherit your
   shell's environment** — the single most common reason a job that works by
   hand fails at 2am.
2. `chmod 600 scheduler.conf` if it holds a password.
3. Copy an entry from `crontab.example` into `crontab -e`, fixing the project
   and interpreter paths.
4. Run it once by hand and check the log.
5. Open the Scheduler app and confirm the tasks appear.

## Safety properties

* **Idempotent** — rent bills are skipped if one already exists for that
  user/shop/month; penalties are recomputed rather than incremented. Running
  twice changes nothing.
* **Nothing silently missed** — expected runs are written to the database
  *before* they are due, so a run that never happened is a PENDING row with a
  past timestamp, visible on the dashboard and picked up on the next sweep.
* **Failure isolation** — one task raising does not stop the others; it becomes
  a FAILED row with its traceback.
* **Serialised** — a database lock stops an overlapping run, or the admin's
  manual button, from double-billing.
