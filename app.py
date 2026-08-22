"""
app.py - Main FastAPI Application
Tenant Management System

This is now the composition root only: it builds the FastAPI app, wires in
every router, and hosts the one subsystem that specifically needs to stay
here (see below). Everything else - schemas, auth, audit logging, business
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

Rent-bill generation (lock, generation logic, nightly scheduler, and the
manual POST /api/bills/generate-rent trigger) deliberately stays in this
file rather than moving to its own module: generate_rent_bills_for_date and
generate_rent_bills_for_date_locked are monkeypatched directly on this
module by tests (tests/test_rent_generation_race.py), and the scheduler
that calls them needs to live right next to them either way.
"""

import os
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import extract
from sqlalchemy.orm import Session

from app_config import APP_TIMEZONE
from db_config import SessionLocal, engine, get_db
from log import get_logger, log_request_middleware
from scheduler_config import load_scheduler_config

# Import ORM models from create_tables so we have a single schema source-of-truth
from create_tables import Bill, Shop, User, UserShop

from auth_service import require_admin
from audit_service import write_audit
from domain_helpers import _decimal_to_float
import settings_service

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
# Routers - every route in the API except the rent-generation trigger below,
# which stays in this file (see the module docstring for why).
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
# ── Rent-bill generation: lock, generation logic, nightly scheduler, and the
# manual trigger endpoint. Stays in app.py - see the module docstring.
# ══════════════════════════════════════════════════════════════════════════════

_rent_generation_thread_lock = threading.Lock()


@contextmanager
def _rent_generation_lock(db: Session, timeout_seconds: int = 30):
    """
    Make rent-bill generation run one-at-a-time across the whole deployment.

    Two layers, because there are two ways to race:
      - threads in this process  -> a plain threading.Lock
      - other worker processes   -> a MySQL named lock, held by the database

    Why this is needed: the API is served by more than one uvicorn worker, and
    every worker process runs the startup hook, so every worker has its own
    APScheduler firing the same cron. Without a lock they all wake at the same
    second, each reads "no rent bill exists yet", and each inserts one.

    The MySQL lock is taken on its OWN connection, not on `db`. A named lock
    belongs to the connection that took it, and the generation run commits
    part-way through — which hands the session's connection back to the pool.
    Releasing on a different connection would silently fail and leave the lock
    stuck until that pooled connection was recycled, blocking every later run.
    """
    # ── Layer 1: other threads in this process ──
    if not _rent_generation_thread_lock.acquire(timeout=timeout_seconds):
        logger.warning("Rent bill generation skipped: busy in this process.")
        raise _RentGenerationBusy()

    conn = None
    db_lock_acquired = False
    try:
        # ── Layer 2: other worker processes, via the database ──
        if db.get_bind().dialect.name == "mysql":
            conn = engine.connect()
            db_lock_acquired = bool(
                conn.exec_driver_sql(
                    "SELECT GET_LOCK('tms_rent_bill_generation', %s)", (timeout_seconds,)
                ).scalar()
            )
            if not db_lock_acquired:
                # Another worker is generating right now. Its run covers the
                # same date, so doing nothing here is the correct outcome.
                logger.warning(
                    "Rent bill generation skipped: another process holds the lock "
                    "(waited %ss).", timeout_seconds,
                )
                raise _RentGenerationBusy()
        yield
    finally:
        try:
            if conn is not None:
                if db_lock_acquired:
                    conn.exec_driver_sql("SELECT RELEASE_LOCK('tms_rent_bill_generation')")
                conn.close()
        except Exception:
            logger.exception("Could not release the rent generation lock")
        finally:
            _rent_generation_thread_lock.release()


class _RentGenerationBusy(Exception):
    """Raised when another process is already generating rent bills."""


def generate_rent_bills_for_date_locked(db: Session, target_date: date) -> dict:
    """
    generate_rent_bills_for_date, serialised. Every caller should use this —
    the scheduler and the manual admin endpoint alike.
    """
    try:
        with _rent_generation_lock(db):
            return generate_rent_bills_for_date(db, target_date)
    except _RentGenerationBusy:
        return {
            "date": target_date.isoformat(),
            "users_matched": 0,
            "created": [],
            "skipped_existing": 0,
            "skipped_zero_rent": 0,
            "skipped_no_shops": 0,
            "skipped_locked": True,
            "message": "Another run is already in progress; nothing was generated twice.",
        }


def generate_rent_bills_for_date(db: Session, target_date: date) -> dict:
    """
    Auto-generate Rent bills for every active user who has opted into
    auto_rent_bill_enabled and whose rent_bill_date matches the day-of-month
    of target_date, one bill per shop currently assigned to them.

    A user with auto_rent_bill_enabled=False is skipped entirely, even if
    rent_bill_date matches, since the admin has not opted them in. A user
    who is opted in but currently has no shops assigned produces no bills
    (nothing to bill) — reflected in skipped_no_shops.

    Idempotent: safe to call more than once for the same date (e.g. from
    multiple worker processes, or a manual re-run) — a user/shop that
    already has a Rent bill for that month is skipped, never duplicated.

    IMPORTANT: that guarantee only holds because callers hold the generation
    lock (see _rent_generation_lock). The "does a bill already exist?" check
    below is a read followed by a write; two processes running it at the same
    instant both read "no bill", and both insert. That is exactly how shop 10
    got two identical rent bills on 2026-08-13 — uvicorn runs with
    --workers 2, and every worker starts its own scheduler, so the cron fired
    twice at the same second. Always call this through
    generate_rent_bills_for_date_locked.
    """
    target_dt = datetime.combine(target_date, datetime.min.time())
    summary = {
        "date": target_date.isoformat(),
        "users_matched": 0,
        "created": [],
        "skipped_existing": 0,
        "skipped_zero_rent": 0,
        "skipped_no_shops": 0,
    }

    users = db.query(User).filter(
        User.is_active == True,
        User.auto_rent_bill_enabled == True,
        User.rent_bill_date == target_date.day,
    ).all()
    summary["users_matched"] = len(users)

    for user in users:
        user_shops = db.query(UserShop).filter(UserShop.user_id == user.id).all()
        if not user_shops:
            summary["skipped_no_shops"] += 1
            continue
        for user_shop in user_shops:
            shop = db.query(Shop).filter(Shop.id == user_shop.shop_id).first()
            if not shop:
                continue

            already_exists = db.query(Bill).filter(
                Bill.user_id == user.id,
                Bill.shop_id == shop.id,
                Bill.bill_type == "Rent",
                extract("year", Bill.bill_date) == target_date.year,
                extract("month", Bill.bill_date) == target_date.month,
            ).first()
            if already_exists:
                summary["skipped_existing"] += 1
                continue

            amount_value = _decimal_to_float(shop.shop_rent)
            if amount_value <= 0:
                summary["skipped_zero_rent"] += 1
                continue

            amount = Decimal(str(amount_value))

            # Calculate due_date for auto-generated rent bills
            # Due Date = Bill Date + Admin-Configured Due Days
            due_days = settings_service.get(db, "bill.due_days")
            due_date_value = target_dt + timedelta(days=due_days)

            bill = Bill(
                user_id        = user.id,
                shop_id        = shop.id,
                bill_type      = "Rent",
                description    = "Auto-generated monthly rent",
                amount         = amount,
                paid_amount    = Decimal("0"),
                pending_amount = amount,
                bill_date      = target_dt,
                due_date       = due_date_value,
                status         = "pending",
            )
            db.add(bill)
            db.flush()

            write_audit(
                db, None, "AUTO_GENERATE", "bills", bill.id,
                new_data={"user_id": user.id, "shop_id": shop.id, "amount": float(amount),
                          "bill_date": target_dt.isoformat()},
            )
            summary["created"].append(bill.id)

    db.commit()
    return summary


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
    return generate_rent_bills_for_date_locked(db, target_date)


def _run_scheduled_rent_bill_generation():
    """Entry point for the APScheduler job: owns its own DB session since it
    runs outside request scope."""
    db = SessionLocal()
    try:
        target_date = datetime.now(ZoneInfo(APP_TIMEZONE)).date()
        summary = generate_rent_bills_for_date_locked(db, target_date)
        logger.info("Scheduled rent bill generation for %s: %s", target_date, summary)
    except Exception:
        db.rollback()
        logger.exception("Scheduled rent bill generation failed")
    finally:
        db.close()


scheduler = BackgroundScheduler(timezone=APP_TIMEZONE)


@app.on_event("startup")
def _start_rent_bill_scheduler():
    """Reads conf/scheduler.conf and (if enabled) schedules the nightly rent
    bill job accordingly. See scheduler_config.py for defaults/fallback
    behavior if the conf file is missing or invalid."""
    config = load_scheduler_config()

    # Every uvicorn worker runs this startup hook, so with --workers 2 you get
    # two schedulers firing the same cron at the same second. The generation
    # lock makes that harmless, but there's no reason to do the work twice:
    # set RUN_SCHEDULER=0 on the API workers and run one dedicated scheduler
    # process, so exactly one is alive.
    if os.getenv("RUN_SCHEDULER", "1").strip().lower() in ("0", "false", "no"):
        logger.info("Scheduler disabled for this process via RUN_SCHEDULER — no jobs will run")
        return

    if not config["scheduler_enabled"]:
        logger.info("Scheduler disabled via conf/scheduler.conf ([scheduler] enabled = false) — no jobs will run")
        return

    job = config["rent_bill_generation"]
    if not job["enabled"]:
        logger.info("Rent bill generation job disabled via conf/scheduler.conf — skipping")
        return

    scheduler.add_job(
        _run_scheduled_rent_bill_generation,
        CronTrigger(
            minute=job["minute"],
            hour=job["hour"],
            day=job["day"],
            month=job["month"],
            day_of_week=job["day_of_week"],
            timezone=job["timezone"],
        ),
        id="generate_rent_bills",
        replace_existing=True,
        misfire_grace_time=job["misfire_grace_time"],
    )
    scheduler.start()
    logger.info(
        "Rent bill generation scheduler started (cron minute=%s hour=%s day=%s month=%s day_of_week=%s %s)",
        job["minute"], job["hour"], job["day"], job["month"], job["day_of_week"], job["timezone"],
    )


@app.on_event("shutdown")
def _stop_rent_bill_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint (for direct `python app.py` execution)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
