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
│   ├── rent_billing.py   monthly rent bill generation
│   ├── penalty_billing.py late-payment penalty engine
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
├── scheduler/          Standalone cron app — see scheduler/README.md
├── tests/              pytest suite (247 tests)
├── docs/               longer-form notes
├── logs/               runtime logs (gitignored)
└── uploads/            meter photos (gitignored)
```

## Where does my change go?

| I want to… | Edit |
|---|---|
| add or change an endpoint | `routers/<resource>.py` |
| change a business rule (how a bill/penalty/reading is computed) | `services/<area>.py` |
| add a column or table | `models/schema.py`, then `python -m models.schema` |
| change a request/response shape | `schemas/api.py` |
| add an admin-configurable setting | `DEFAULTS` in `services/settings.py` |
| change auth or role rules | `core/security.py` |
| change a scheduled job | `scheduler/` (runs from cron, never from the app) |

## Import rules

Dependencies point one way — nothing lower ever imports something higher:

```
routers  →  services  →  models / core
     ↘  helpers  ↗           schemas
```

A router may import anything. A service may import `core`, `models`, `schemas`
and other services. `core` and `models` import nothing from this project except
each other.

## Running

```bash
python -m models.schema                 # create tables + seed
uvicorn app:app --reload                # dev
pytest -q                               # tests (throwaway SQLite, touches no real data)
```

Docker builds and runs the same two steps — see the `CMD` in `Dockerfile`.
