# Tenant Management System — Backend

FastAPI + MySQL. This file is the map: it says where each kind of change goes,
so you edit one small module instead of hunting through a big one.

## Layout

```
shopmanagement/
├── app.py              Composition root — builds the app, includes routers. Nothing else.
├── requirements.txt
├── Dockerfile          One COPY per package (adding a module needs no Dockerfile edit)
│
├── core/               Cross-cutting infrastructure
│   ├── config.py         app constants (timezone, upload limits, …)
│   ├── database.py       engine, SessionLocal, Base, get_db
│   ├── logger.py         logging setup, get_logger()
│   └── security.py       password hashing, JWT, require_admin / require_tenant
│
├── models/
│   └── schema.py       every SQLAlchemy model + `python -m models.schema` to create/seed
│
├── schemas/
│   └── api.py          Pydantic request/response models
│
├── services/           Business logic — no HTTP, no FastAPI imports
│   ├── settings.py       runtime config in the DB (DEFAULTS = source of truth)
│   ├── meter.py          submeter rules: upload window, photo policy, readings
│   ├── photo_storage.py  meter-photo files on disk
│   ├── razorpay.py       online payments (orders, verification, webhooks)
│   └── audit.py          audit-trail writes
│
├── helpers/            Small shared helpers used by routers
│   ├── domain.py         bill/payment reconciliation, financial summaries
│   └── meter.py          reading serialisation, tenant shop lookup
│
├── routers/            HTTP endpoints — one module per resource
│   auth, users, shops, complexes, bills, payments, deposit_payments,
│   meters, meter_tariffs, meter_readings, tenant_meters, tenant_portal,
│   razorpay, reports, dashboard, ledger, search, audit_log, scheduler_admin
│
├── scheduler/          Two standalone cron scripts — see docs/SCHEDULER.md
│   ├── auto_rent_generation/   auto_rent_generation.py · db_config.py · logs/
│   └── due_bill_penalty/       due_bill_penalty.py · db_config.py · logs/
├── tests/              pytest suite
├── docs/               longer-form notes
├── logs/               runtime logs (gitignored)
└── uploads/            meter photos (gitignored)
```

## Where does my change go?

| I want to… | Edit |
|---|---|
| add or change an endpoint | `routers/<resource>.py` |
| change a business rule (how a reading or a payment is computed) | `services/<area>.py` |
| change the rent or penalty rules | the relevant `scheduler/*/` script — self-contained, one file each |
| add a column or table | `models/schema.py`, then `python -m models.schema` |
| change a request/response shape | `schemas/api.py` |
| add an admin-configurable setting | `DEFAULTS` in `services/settings.py` |
| change auth or role rules | `core/security.py` |
| change a scheduled job | the relevant `scheduler/*/` script (cron runs it, the app never does) |
| change a scheduler switch or its bounds | `DEFAULTS` in `services/settings.py`, and the matching fallback in the script |
| see what a scheduler did, and why | Scheduler screen → `routers/scheduler_tracking.py` |

## Import rules

Dependencies point one way — nothing lower ever imports something higher:

```
routers  →  services  →  models / core
     ↘  helpers  ↗           schemas

scheduler/*/  →  the database        (imports nothing from this project)
```

A router may import anything. A service may import `core`, `models`, `schemas`
and other services. `core` and `models` import nothing from this project except
each other.

`scheduler/` holds two standalone cron scripts. They import nothing from this
project and this project imports nothing from them — the database is the only
thing the two sides share. The scripts write what they did to
`scheduler_runs` / `scheduler_run_items`; the app reads that back through
`routers/scheduler_tracking.py`. See `docs/SCHEDULER.md`.

## Running

```bash
python -m models.schema                 # create tables + seed
uvicorn app:app --reload                # dev
pytest -q                               # tests (throwaway SQLite)
```

Docker builds and runs the same two steps — see the `CMD` in `Dockerfile`.
