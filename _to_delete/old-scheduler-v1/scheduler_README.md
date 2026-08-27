# scheduler/

Every scheduled job for the Tenant Management System, run by **cron**.

**Standalone.** Nothing in this folder imports the application — not its
config, not its logging, not its models, not its services. Copy this folder to
a machine on its own, `pip install -r requirements.txt`, point `scheduler.conf`
at the database, and it runs. The only thing the two services share is the
database.

The dependency runs one way only: the **application imports this folder** (for
the admin's "Generate rent now" button, the penalty figures shown to tenants,
and the Scheduler settings screen). This folder imports nothing back. A test
enforces that — see `tests/test_independence.py`.

---

## What is in here

| File | Purpose |
| --- | --- |
| `scheduler.conf` | **The one config file.** Database, timezone, logging. Nothing else. |
| `config.py` | Reads it (environment wins over the file). |
| `db.py` | Engine and session, plus the startup schema checks. |
| `models.py` | Its own ORM mapping — see *Owned vs mirrored* below. |
| `settings.py` | The `scheduler.*` switches, read from the shared database. |
| `logging_setup.py` | One log file per task. |
| `errors.py` | Error vocabulary and the exit codes every script uses. |
| `money.py` | What a bill owes. |
| `billing/rent.py` | Rent generation rules. |
| `billing/penalty.py` | Late-payment penalty rules. |
| `service.py` | The ledger: task registry, statuses, due/missed queries. No business logic. |
| `master.py` | The coordinator. Finds due tasks, runs each in isolation, records results. |
| `tasks/` | One thin module per task. |
| `run_scheduler.py` | The master sweep — one entry that covers everything. |
| `run_rent_generation.py` | Rent Generation, on its own. |
| `run_due_date_penalty.py` | Due Date Penalty, on its own. |
| `run_future_task_checker.py` | Future Task Checker, on its own. |
| `task_runner.py` | Shared plumbing behind the three per-task scripts. |
| `tests/` | Its own suite — passes with the application absent. |
| `logs/` | One file per task (see below). |

---

## Install

```bash
pip install -r scheduler/requirements.txt
```

Four packages: SQLAlchemy, PyMySQL, cryptography, python-dotenv. No FastAPI, no
uvicorn, no auth stack — the scheduler is not a web service.

Then edit `scheduler.conf` with the database connection, and:

```bash
chmod 600 scheduler.conf      # it can hold a password
```

Nothing else to set up. The scheduler creates its own `scheduler_tasks` table
on first run.

---

## Running

Two options. Pick **A** or **B**, not both — every task is idempotent so
nothing breaks either way, but two things picking up the same occurrence makes
the run log confusing.

**Option A — one sweep does everything** (simplest):

```bash
python -m scheduler.run_scheduler master
```

**Option B — each task on its own schedule:**

```bash
python -m scheduler.run_future_task_checker    # run this one first
python -m scheduler.run_rent_generation
python -m scheduler.run_due_date_penalty
```

If you split them up, run the future-task checker first: it is what registers
upcoming runs and finds ones that were missed.

Backfill a specific day with any of them:

```bash
python -m scheduler.run_rent_generation --date 2026-08-01
```

Run as a module (`-m`) from the folder *containing* `scheduler/`. Running the
script directly works too — `python scheduler/run_rent_generation.py` — because
`_bootstrap.py` puts the right directory on `sys.path`.

See `crontab.example` for the crontab entries.

---

## Logs

One file per task, in `logs/` (configurable — see `[logging]` in
`scheduler.conf`):

```
logs/rent_generation.log        everything the Rent Generation task did
logs/due_date_penalty.log       everything the Due Date Penalty task did
logs/future_task_checker.log    everything the Future Task Checker did
logs/master.log                 the coordinator: which tasks got a turn
logs/errors.log                 ERROR and above, from all of them
```

"What happened to the penalty run last night" is one file, not a grep through
a shared log. `errors.log` is the exception on purpose: per-task files answer
*what did this task do*, a single error file answers *did anything break*,
which is the question monitoring actually asks. Nothing is only in
`errors.log` — every line there is also in its own task's file.

Files rotate at midnight and are gzipped; 30 days are kept by default.

A new task added to the registry gets its own log file the first time it runs.
Nothing to wire up.

---

## Settings

The on/off switches are **not** in `scheduler.conf`. They live in the database
and are edited on the Scheduler settings screen in the app:

| Setting | Default |
| --- | --- |
| Master scheduler | on |
| Automatic rent generation | on |
| Due-date penalty | **off** — turning it on starts charging tenants |
| Penalty per day (% of the original bill) | 1.0 |
| Grace period (days) | 0 |
| Maximum penalty per bill | 0 (no cap) |
| Look back for missed runs (days) | 30 |
| Register future runs (days ahead) | 7 |

They are declared in `settings.py`, in this folder, and the app imports that
declaration. A switch in a file on the server *and* a toggle in the UI would be
two answers to the same question, and whichever the operator checked first
would be the wrong one.

Read fresh on every run, so a change takes effect on the next tick — nothing to
restart, no crontab to edit.

Turning the master switch off does not make the scheduler invisible: cron still
fires, the sweep still runs, and every due occurrence is recorded as **SKIPPED**
with the reason, so the dashboard shows exactly what did not happen.

---

## Owned vs mirrored tables

`models.py` maps two kinds of table, and the difference matters:

- **Owned** — `scheduler_tasks`. The scheduler created it, is its only writer,
  and creates it on demand. Nothing needs running by hand on a fresh install.
- **Mirrored** — `users`, `shops`, `user_shops`, `bills`, `payments`,
  `app_settings`, `audit_logs`. These belong to the application. The scheduler
  maps only the columns it uses and **never creates or alters them** — a
  scheduler pointed at the wrong database must not quietly invent an empty
  `bills` and start billing nobody.

Mapping a subset is safe for reading: a column added by the app is simply not
fetched. It is `INSERT`s that care. So on every run `db.py` calls
`models.verify_schema()`, which warns in the log if the app has added a NOT
NULL column with no default that this process does not set — the one upstream
change that would otherwise turn rent generation into an opaque driver error at
02:00.

---

## Two things to keep in step with the app

Independence has a price, and it is exactly two items. Both are covered by
tests in the **application's** suite (`tests/test_scheduler_parity.py`), which
fail if either drifts:

1. **`money.py`** holds this folder's copy of "what does a bill owe" — the rule
   `helpers/domain.py` holds for the app. Change one, change the other.
2. **`timezone`** in `scheduler.conf` must match the app's `APP_TIMEZONE`, or a
   task scheduled for 02:00 shows the wrong hour on the dashboard.

---

## Testing

```bash
pytest scheduler          # 41 tests, no application needed
```

`tests/test_independence.py` is the one that keeps this folder deployable: it
reads every source file looking for an application import, then loads every
module in a subprocess with the app's packages blocked.

---

## Exit codes

Identical across every script, so one monitoring rule covers them all:

| Code | Meaning |
| --- | --- |
| 0 | ran, including "ran and had nothing to do" |
| 1 | the run itself failed — unreachable database, bad config. See the log. |
| 2 | skipped: switched off in Scheduler settings |
| 3 | bad usage — unknown job name, unparseable date |

A failing **task** is not a failing **run**. The task is recorded as FAILED
with its traceback and is retryable from the dashboard; the process still exits
0, because cron mailing about something the dashboard already owns is noise.

---

## Why cron, and not the application

The API runs under `uvicorn --workers 2`, and every worker ran the FastAPI
startup hook — so every worker started its own APScheduler and the same nightly
job fired twice at the same second. The database lock made that harmless, but
it was work done twice for no reason, and "how many times did the job run last
night?" depended on how many workers happened to be up.

As a cron entry it runs exactly once, where the rest of the box's scheduled
work already lives, and it can be run by hand, backfilled, or restarted without
touching the API.
