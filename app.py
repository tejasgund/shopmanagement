"""
app.py - Main FastAPI Application
Tenant Management System

This is now the composition root only: it builds the FastAPI app, wires in
every router, and hosts the admin's manual rent-generation trigger.
Everything else - schemas, auth, audit logging, business
logic, and every route - lives in its own module under routers/ or in one
of the *_service.py / *_helpers.py files alongside this one. See each
module's docstring for exactly which "step" of the router/service split
moved it here; this file is the last step (25): wiring routers/ in and
retiring the duplicate code that used to live in this file alongside them.

Features:
    - JWT Authentication (HS256)
    - Role-Based Access Control (admin / tenant)
    - Full CRUD for complexes, shops, users
    - Bill management with auto payment reconciliation
    - Tenant read-only portal
    - Audit logging on every mutating operation
    - Swagger / ReDoc documentation at /docs and /redoc

This file no longer runs any background work. Rent-bill generation lives in
rent_billing.py, and it is driven by cron via scheduler/run_scheduler.py -
NOT by an in-process timer. The app previously started an APScheduler inside
every uvicorn worker, which meant `--workers 2` ran the same nightly job
twice at the same second; moving it to cron makes "exactly one run" a
property of the deployment rather than something the app has to defend
against at runtime.

The only rent-billing thing left here is the admin's manual trigger endpoint,
which delegates to the same rent_billing function the cron job calls.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# Load .env before anything below reads os.getenv() - only fills in variables
# that aren't already set, so Docker/systemd env injection still wins in
# production. This must happen before any of this project's own modules are
# imported below: auth_service reads JWT_SECRET, db_config reads DB_*, and
# razorpay_service reads RAZORPAY_* at import time, so .env has to be loaded
# first or those modules would see the un-injected defaults.
from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app_config import APP_TIMEZONE
from db_config import get_db
from log import get_logger, log_request_middleware

# Import ORM models from create_tables so we have a single schema source-of-truth
from create_tables import User

from auth_service import require_admin
import rent_billing

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
from routers.scheduler_admin import router as scheduler_admin_router
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
app.include_router(scheduler_admin_router)


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
# ── Rent-bill generation: the admin's manual trigger only.
#
# The logic itself lives in rent_billing.py, and the automatic nightly run is
# a cron job (see scheduler/) rather than an in-process timer. This endpoint
# and that cron job call the same function, so pressing the button and waiting
# for the nightly run can never produce different bills.
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/bills/generate-rent", tags=["Bill"])
def generate_rent_bills(
    date: Optional[str] = Query(None, description="YYYY-MM-DD. Defaults to today (Asia/Kolkata)."),
    db:   Session = Depends(get_db),
    _:    User    = Depends(require_admin),
):
    """
    Manually trigger Rent bill generation for a given day (defaults to
    today). This is the same logic the automatic nightly scheduler runs —
    useful for on-demand runs, testing, or backfilling a date the scheduler
    missed. Safe to call repeatedly; already-generated bills are skipped.
    Admin only.
    """
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, detail="date must be in YYYY-MM-DD format")
    else:
        target_date = datetime.now(ZoneInfo(APP_TIMEZONE)).date()

    # Locked, same as the scheduler: two admins pressing the button together,
    # or a press landing while the nightly job runs, must not double-bill.
    return rent_billing.generate_rent_bills_for_date_locked(db, target_date)


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint (for direct `python app.py` execution)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
