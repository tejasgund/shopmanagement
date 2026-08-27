# Scheduler

Two standalone scripts, run by **cron**. Each one connects to the database,
does its job, records what it did, and exits.

```
scheduler/
├── auto_rent_generation/
│   ├── auto_rent_generation.py     creates each tenant's monthly Rent bill
│   ├── db_config.py                ← put your database details here
│   └── logs/                       auto_rent_generation.log · errors.log
└── due_bill_penalty/
    ├── due_bill_penalty.py         applies the daily late fee to overdue bills
    ├── db_config.py                ← put your database details here
    └── logs/                       due_bill_penalty.log · errors.log
```

That is the whole scheduler. No shared helpers, no framework, no imports from
the application. Each script imports its own `db_config.py` and nothing else
from this project.

---

## Setup

**1. Install the one dependency:**

```bash
pip install pymysql
```

**2. Fill in both `db_config.py` files** with your real values:

```python
DB_CONFIG = {
    "host":     "172.31.52.221",
    "port":     3306,
    "database": "tenant_management",
    "user":     "your_user",
    "password": "your_password",
}
```

Both normally hold the *same* credentials — they work on the same database.
They are two separate files so either scheduler can later be pointed at a
different host or credential without touching the other.

**3. Lock them down.** They hold a password:

```bash
chmod 600 scheduler/auto_rent_generation/db_config.py
chmod 600 scheduler/due_bill_penalty/db_config.py
```

**4. Create the tracking tables** (once, from the application):

```bash
python -m models.schema
```

This also adds `bills.rent_period` and the unique index that makes duplicate
rent bills impossible. If any tenant/shop already has two Rent bills in one
month, it will *tell you which* rather than failing — clean those up and run
it again.

---

## Cron

```cron
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Rent generation — 02:00 daily
0 2 * * * cd /opt/shopmanagement/scheduler/auto_rent_generation && /usr/bin/python3 auto_rent_generation.py

# Due-date penalty — 02:15 daily
15 2 * * * cd /opt/shopmanagement/scheduler/due_bill_penalty && /usr/bin/python3 due_bill_penalty.py
```

`cd` into the script's own folder first: that is how each finds its
`db_config.py`.

Both scripts write their own rotated log files, so no `>> logfile` redirect is
needed. Cron will email anything printed to stdout — which is the summary
line, and any failure. To silence that, append `> /dev/null 2>&1`; the log
files still get everything.

Run them daily. Both are safe to run more than once a day, and safe to run
late.

---

## Running by hand

```bash
cd scheduler/auto_rent_generation
python3 auto_rent_generation.py                      # today
python3 auto_rent_generation.py --date 2026-09-05    # as if run that day
python3 auto_rent_generation.py --dry-run            # decide, write nothing
python3 auto_rent_generation.py --manual             # tag the run as manual
```

`--dry-run` is the safe way to see what tonight would do. It reads everything,
decides everything, logs everything, and writes nothing.

---

## Exit codes

Identical for both scripts, so one monitoring rule covers them:

| Code | Meaning |
| --- | --- |
| 0 | ran — including "ran and had nothing to do" |
| 1 | the run failed (no database, bad config). See the log. |
| 2 | switched off in Settings; nothing attempted |
| 3 | bad usage (unparseable `--date`) |
| 4 | another run holds the lock; that run covers the same work |

A single tenant failing is **not** exit 1. That tenant is recorded as FAILED,
the rest are still billed, and the run reports `PARTIAL`. Blocking the whole
night's billing because one row is malformed would be the wrong trade.

---

## Logs

```
scheduler/auto_rent_generation/logs/auto_rent_generation.log
scheduler/auto_rent_generation/logs/errors.log
scheduler/due_bill_penalty/logs/due_bill_penalty.log
scheduler/due_bill_penalty/logs/errors.log
```

Each scheduler writes only into its own folder. `errors.log` carries the same
lines at ERROR and above — per-scheduler files answer *what did it do*, the
error file answers *did anything break*. Rotated at midnight, gzipped, 30 days
kept.

---

## Settings

The rules are in the database, edited on the **Scheduler → Settings** screen,
not in the scripts:

| Setting | Default |
| --- | --- |
| Automatic rent generation | on |
| Due-date penalty | **off** — turning it on starts charging tenants |
| Penalty per day (% of the original bill) | 1.0 |
| Grace period (days) | 0 |
| Maximum penalty per bill | 0 (no cap) |

Read fresh at the start of every run, so a change takes effect on the next one.
Nothing to restart, no crontab to edit.

The scripts carry the same values as a fallback for a database where a setting
has never been saved. `tests/test_scheduler_tracking.py` checks the two copies
still agree.

---

## How duplicate rent is prevented

Three layers. The first is the one that actually guarantees it:

1. **`bills.rent_period` + the UNIQUE index** on `(user_id, shop_id,
   rent_period)`. A second Rent bill for the same tenant, shop and month is
   refused by the database — whatever tries it, including the admin's manual
   bill screen.
2. **A MySQL named lock** held for the whole run, so two cron entries or an
   overlapping manual run cannot interleave.
3. **An existence check** in the script, which turns "refused" into a tidy
   `SKIPPED_DUPLICATE` tracking row instead of an error.

You can see layer 3 working on the **Rent** tab: skipped duplicates are listed,
not hidden.

---

## Missed days

A tenant is billed when their rent day **has arrived** this month and they have
no rent bill for it — not only when their rent day is exactly today.

So if the server is down on the 5th, the run on the 6th picks up everyone whose
rent day was the 5th. The bill is still dated the 5th, so the due date and any
later penalty are the ones the tenant was always going to get: the run being
late does not cost them days. The tracking row says so — *"Created on 2026-09-07
— later than the rent day, so this was a catch-up."*

---

## Why penalties can be re-run safely

The penalty is **recomputed from scratch** every run, never incremented. The
answer depends only on (original amount, due date, as-of date, settings), so
running twice in a day, retrying, or backfilling an earlier date all converge
on the same number.

`bills.amount` is never touched. The penalty lives in `penalty_amount`, so
"what was this bill for" and "what is owed now" stay separately answerable.

---

## Where the tracking goes

```
scheduler_runs        one row per execution — run ID, status, counts, totals
scheduler_run_items   one row per customer/bill touched, with the reason why
```

The scripts write. `routers/scheduler_tracking.py` reads. The frontend reads
that. Nothing in the API can start a run — cron is the only thing that decides
when a bill is raised.

`scheduler_run_items.reason` holds the sentence the script wrote at the moment
it decided, in the settings that applied then. That is what the Penalty screen
shows, and it is stored rather than recomputed so that changing the rate later
never rewrites what a tenant was actually charged.
