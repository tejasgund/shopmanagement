"""
app.py - Main FastAPI Application
Tenant Management System

This is the composition root only: it builds the FastAPI app, wires in every
router, and hosts the admin's manual rent-generation trigger. Everything else
lives in a package next to this file, one concern per package:

    core/      configuration, database, logging, security (JWT/roles)
    models/    SQLAlchemy models and the schema-creation entry point
    schemas/   Pydantic request/response models
    services/  business logic: settings, rent billing, penalties, meters,
               photo storage, Razorpay, audit
    helpers/   small shared helpers used by routers
    routers/   HTTP endpoints, one module per resource
    scheduler/ two standalone cron scripts - NOT imported from here

To change one thing you edit one module: a route in routers/, a rule in
services/, a column in models/schema.py. Nothing here needs touching except
when a whole new router is added.

Features:
    - JWT Authentication (HS256)
    - Role-Based Access Control (admin / tenant)
    - Full CRUD for complexes, shops, users
    - Bill management with auto payment reconciliation
    - Tenant read-only portal
    - Audit logging on every mutating operation
    - Swagger / ReDoc documentation at /docs and /redoc

This file runs no background work, and imports nothing from scheduler/.

The two schedulers are standalone scripts run by cron
(scheduler/auto_rent_generation/ and scheduler/due_bill_penalty/). They talk
to the database and nothing else. This application reads back what they
recorded, through routers/scheduler_tracking.py, and the two sides share
nothing but the database.

There is deliberately no "generate rent now" endpoint any more. Rent
generation is cron's job and only cron's, so there is exactly one thing that
decides when a bill is raised; an admin who needs a one-off bill creates it
through the ordinary bill screen, which is unchanged.
"""

from datetime import datetime

# Load .env before anything below reads os.getenv() - only fills in variables
# that aren't already set, so Docker/systemd env injection still wins in
# production. This must happen before any of this project's own modules are
# imported below: core/security.py reads JWT_SECRET, core/database.py reads
# DB_*, and services/razorpay.py reads RAZORPAY_* at import time, so .env has
# to be loaded first or those modules would see the un-injected defaults.
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.logger import get_logger, log_request_middleware

# Import ORM models from create_tables so we have a single schema source-of-truth

from routers.audit_log import router as audit_log_router
from routers.auth import router as auth_router
from routers.bills import router as bills_router
from routers.complexes import router as complexes_router
from routers.dashboard import router as dashboard_router
from routers.deposit_payments import router as deposit_payments_router
from routers.ledger import router as ledger_router
from routers.meter_readings import router as meter_readings_router
from routers.meter_tariffs import router as meter_tariffs_router
from routers.meters import router as meters_router
from routers.payments import router as payments_router
from routers.razorpay import router as razorpay_router
from routers.reports import router as reports_router
from routers.scheduler_tracking import router as scheduler_tracking_router
from routers.search import router as search_router
from routers.settings import router as settings_router
from routers.shops import router as shops_router
from routers.tenant_meters import router as tenant_meters_router
from routers.tenant_portal import router as tenant_portal_router
from routers.users import router as users_router

# ──────────────────────────────────────────────
# Logger
# ──────────────────────────────────────────────
logger = get_logger("app")

# ══════════════════════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════════════════════
app = FastAPI(
    title="Tenant Management System",
    description="REST API for managing tenants, shops, complexes, bills, and payments.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ──────────────────────────────────────────────
# CORS Configuration - Allow frontend access
# ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development - allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, PUT, DELETE, OPTIONS)
    allow_headers=["*"],  # Allows all headers
)

# Register request-logging middleware
app.middleware("http")(log_request_middleware)


@app.on_event("startup")
def warn_if_schema_is_behind():
    """
    Say plainly, once, if the database is missing columns this build needs.

    Without this a pending migration shows up as a 500 on every bill screen and
    an "Unknown column 'bills.parent_bill_id'" buried in the log - which reads
    as "the finance section is broken", not as "one command has not been run".
    The whole admin area can go dark over a column that takes a second to add.

    Deliberately only WARNS. It does not alter the schema: the API runs under
    several uvicorn workers, and having each of them race to run DDL on startup
    is a worse problem than the one it would solve. Migrations stay an explicit
    step:

        python -m models.schema
    """
    try:
        from sqlalchemy import inspect as _sa_inspect

        from core.database import engine
        from models.schema import Base

        inspector = _sa_inspect(engine)
        live_tables = set(inspector.get_table_names())
        missing = []

        for table in Base.metadata.sorted_tables:
            if table.name not in live_tables:
                missing.append(f"table {table.name} (absent)")
                continue
            live_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in live_cols:
                    missing.append(f"{table.name}.{column.name}")

        if missing:
            logger.error(
                "DATABASE SCHEMA IS BEHIND THIS BUILD - %s missing: %s. "
                "Endpoints touching these tables will fail until you run: "
                "python -m models.schema",
                len(missing), ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""),
            )
        else:
            logger.info("Schema check: database matches this build.")
    except Exception as exc:
        # A check that cannot run must never stop the app from starting.
        logger.warning("Could not check the database schema on startup: %s", exc)

# ══════════════════════════════════════════════════════════════════════════════
# Routers - every route in the API except the manual rent-generation trigger
# below, which stays in this file next to nothing else.
# ══════════════════════════════════════════════════════════════════════════════
app.include_router(auth_router)
app.include_router(complexes_router)
app.include_router(shops_router)
app.include_router(users_router)
app.include_router(bills_router)
app.include_router(payments_router)
app.include_router(deposit_payments_router)
app.include_router(search_router)
app.include_router(reports_router)
app.include_router(tenant_portal_router)
app.include_router(razorpay_router)
app.include_router(audit_log_router)
app.include_router(ledger_router)
app.include_router(dashboard_router)
app.include_router(meters_router)
app.include_router(meter_tariffs_router)
app.include_router(tenant_meters_router)
app.include_router(meter_readings_router)
app.include_router(settings_router)
app.include_router(scheduler_tracking_router)


# ══════════════════════════════════════════════════════════════════════════════
# Global exception handler – ensures JSON errors are always returned
# ══════════════════════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "detail": "Internal server error"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint (for direct `python app.py` execution)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
