"""
app.py - Main FastAPI Application
Tenant Management System

Features:
    - JWT Authentication (HS256)
    - Role-Based Access Control (admin / tenant)
    - Full CRUD for complexes, shops, users
    - Bill management with auto payment reconciliation
    - Tenant read-only portal
    - Audit logging on every mutating operation
    - Swagger / ReDoc documentation at /docs and /redoc
"""

import json
import os
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from zoneinfo import ZoneInfo

# Load .env before anything below reads os.getenv() - only fills in variables
# that aren't already set, so Docker/systemd env injection still wins in
# production. This is what finally makes the python-dotenv dependency do
# something; previously .env only worked via `docker run --env-file`.
from dotenv import load_dotenv
load_dotenv()

import bcrypt
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status,
)
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

from db_config import SessionLocal, engine, get_db
from log import get_logger, log_request_middleware
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import extract
from scheduler_config import load_scheduler_config

from app_config import APP_TIMEZONE



# Import ORM models from create_tables so we have a single schema source-of-truth
from create_tables import (
    AppSetting, AuditLog, Bill, Complex, DepositPayment, Meter, MeterReading,
    MeterTariff, Payment, RazorpayOrder, Shop, User, UserShop,
)

# Submeter reading feature: business rules, photo storage and runtime settings
# live in their own modules so app.py stays a routing/serialisation layer.
import meter_service
import photo_storage
import settings_service
from meter_service import MeterError

# Razorpay Standard Checkout (online tenant payments)
import razorpay

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

# ──────────────────────────────────────────────
# Routers extracted out of this file (router/service split, in progress -
# each one is pulled out only after its own dependencies are also extracted,
# so nothing here ever imports back from app.py).
# ──────────────────────────────────────────────
from routers import audit_log as _audit_log_router
from routers import reports as _reports_router
from routers import search as _search_router
from routers import dashboard as _dashboard_router
from routers import ledger as _ledger_router
from routers import meter_tariffs as _meter_tariffs_router
from routers import meters as _meters_router
from routers import tenant_meters as _tenant_meters_router
from routers import meter_readings as _meter_readings_router
from routers import settings as _settings_router
from routers import complexes as _complexes_router
from routers import shops as _shops_router
from routers import users as _users_router
from routers import bills as _bills_router
from routers import payments as _payments_router
from routers import deposit_payments as _deposit_payments_router
from routers import tenant_portal as _tenant_portal_router
app.include_router(_audit_log_router.router)
app.include_router(_reports_router.router)
app.include_router(_search_router.router)
app.include_router(_dashboard_router.router)
app.include_router(_ledger_router.router)
app.include_router(_meter_tariffs_router.router)
app.include_router(_meters_router.router)
app.include_router(_tenant_meters_router.router)
app.include_router(_meter_readings_router.router)
app.include_router(_settings_router.router)
app.include_router(_complexes_router.router)
app.include_router(_shops_router.router)
app.include_router(_users_router.router)
app.include_router(_bills_router.router)
app.include_router(_payments_router.router)
app.include_router(_deposit_payments_router.router)
app.include_router(_tenant_portal_router.router)

# JWT settings, token helpers and the auth dependencies (get_current_user /
# require_admin / require_tenant) moved to auth_service.py (step 2 of the
# router/service split) - imported just below, after the Razorpay settings
# block that stays here. JWT_SECRET / JWT_ALGORITHM / JWT_EXPIRE_MINUTES /
# security were only ever used inside that moved code, so nothing else in
# this file needs them directly any more.

# ──────────────────────────────────────────────
# Razorpay settings
# The admin-editable values in Settings (payment.razorpay_key_id/_secret) are
# the primary source - see settings_service.py's docstring for why this pair
# is a deliberate exception to "secrets live in env only". These two env vars
# are kept ONLY as a fallback for deployments that still prefer .env (or
# haven't set the DB values yet); see _razorpay_credentials() below, which is
# what every request actually calls. No default: an unset secret must fail
# closed, never silently sign with "".
# ──────────────────────────────────────────────
RAZORPAY_KEY_ID_ENV     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET_ENV = os.getenv("RAZORPAY_KEY_SECRET", "")
# Separate secret used only to verify /api/webhooks/razorpay calls really came
# from Razorpay (HMAC over the raw request body) - not the same value as
# RAZORPAY_KEY_SECRET above, which signs API calls we make outbound.
RAZORPAY_WEBHOOK_SECRET_ENV = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")


# ══════════════════════════════════════════════════════════════════════════════
# JWT HELPERS / AUTH DEPENDENCIES / AUDIT HELPER
# Moved to auth_service.py and audit_service.py (step 2 of the router/service
# split). Re-imported here under their original names so every existing
# reference in this file (and app.<name> in tests) keeps working unchanged.
# ══════════════════════════════════════════════════════════════════════════════

from auth_service import (
    JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_MINUTES, security,
    create_access_token, decode_token,
    get_current_user, require_admin, require_tenant,
)
from audit_service import write_audit


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC SCHEMAS
# Moved to schemas.py (step 1 of the router/service split) - imported below via
# `from schemas import *`. Every name that used to be defined inline here is
# still available as app.<Name> for backward compatibility (tests, response_model=
# references elsewhere in this file, etc.) - only where the class body lives
# has changed, not its shape or behavior.
# ══════════════════════════════════════════════════════════════════════════════

from schemas import (
    LoginRequest, LoginResponse,
    ComplexCreate, ComplexUpdate, ComplexResponse,
    ShopCreate, ShopUpdate, ShopOwnerInfo, ShopResponse, AssignComplexRequest,
    UserCreate, UserUpdate, UserResponse,
    AssignShopsRequest, UpdateAgreementRequest, DetachShopsRequest, ResetPasswordRequest,
    BillCreate, BillResponse, BillUpdate,
    PaymentCreate, PaymentResponse, PaymentUpdate,
    AutoAllocatePreviewRequest, AllocationRow, AutoAllocatePreviewResponse,
    ConfirmAllocationItem, AutoAllocateConfirmRequest, AutoAllocateResult, AutoAllocateResponse,
    DepositPaymentCreate, DepositPaymentResponse, DepositPaymentUpdate,
    RazorpayCreateOrderRequest, RazorpayCreateOrderResponse, RazorpayVerifyRequest,
    MeterCreate, MeterUpdate, TariffCreate,
    VerifyReadingRequest, ApproveReadingRequest, RejectReadingRequest,
    AssignMeterShopRequest, SettingsUpdateRequest,
)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY
# Most of this section moved to domain_helpers.py (step 4 of the router/
# service split) - imported below. _razorpay_credentials/_razorpay_webhook_
# secret/_razorpay_public_config stay HERE, not there: they read the
# RAZORPAY_KEY_ID_ENV/RAZORPAY_KEY_SECRET_ENV/RAZORPAY_WEBHOOK_SECRET_ENV
# module globals above, which several tests monkeypatch directly via
# app_module - moving these functions elsewhere would silently break that
# patching (it would patch this module's copy, never the one actually read).
# ══════════════════════════════════════════════════════════════════════════════

from domain_helpers import (
    _decimal_to_float, _shop_owner_map, _shop_to_response, _reconcile_bill,
    _current_user_shops, _deposit_paid_for_shop, _pending_rent_for_user,
    _build_user_financial_summary, _tenant_payment_dict,
)


def _razorpay_credentials(cfg: dict) -> tuple:
    """
    (key_id, key_secret), preferring the admin-editable Settings values
    (payment.razorpay_key_id/_secret) and falling back to the RAZORPAY_KEY_ID/
    RAZORPAY_KEY_SECRET env vars only where the DB value is blank - lets a
    deployment migrate from .env to Settings whenever it's convenient, not
    all at once.
    """
    key_id = str(cfg.get("payment.razorpay_key_id") or "").strip() or RAZORPAY_KEY_ID_ENV
    key_secret = str(cfg.get("payment.razorpay_key_secret") or "").strip() or RAZORPAY_KEY_SECRET_ENV
    return key_id, key_secret


def _razorpay_webhook_secret(cfg: dict) -> str:
    """
    The secret configured on the Razorpay dashboard's Webhooks page for
    /api/webhooks/razorpay - NOT the same value as the Key Secret (that one
    signs requests we make outbound; this one lets us verify requests
    Razorpay makes inbound). Same Settings-first, env-fallback pattern as
    _razorpay_credentials.
    """
    return str(cfg.get("payment.razorpay_webhook_secret") or "").strip() or RAZORPAY_WEBHOOK_SECRET_ENV


def _razorpay_public_config(cfg: dict) -> tuple:
    """
    (enabled, key_id) as the frontend should see them. Single source of
    truth for both /api/settings/public and the /api/tenant/home bundle, so
    the two can't quietly disagree about whether "Pay online" should show.
    Needs the admin's switch on AND real keys configured (Settings or env) -
    a half-configured server looks "off" to tenants, never errors on them.
    """
    key_id, key_secret = _razorpay_credentials(cfg)
    ready = bool(cfg.get("payment.razorpay_enabled")) and bool(key_id) and bool(key_secret)
    return ready, (key_id if ready else None)


# _shop_owner_map / _shop_to_response / _reconcile_bill / _current_user_shops /
# _deposit_paid_for_shop / _pending_rent_for_user / _build_user_financial_summary
# all moved to domain_helpers.py (imported above).


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: /api/login
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/login", response_model=LoginResponse, tags=["Authentication"])
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with mobile + password and receive a JWT token."""
    user = db.query(User).filter(User.mobile == payload.mobile).first()

    if not user or not bcrypt.checkpw(payload.password.encode(), user.password_hash.encode()):
        raise HTTPException(status_code=401, detail="Invalid mobile or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    token = create_access_token({"sub": str(user.id), "role": user.role})

    write_audit(db, user.id, "LOGIN", "users", user.id)
    db.commit()

    logger.info("LOGIN | user_id=%s | mobile=%s | role=%s", user.id, user.mobile, user.role)
    return LoginResponse(success=True, token=token, role=user.role)


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Complex Management
# Moved to routers/complexes.py (step 14 of the router/service split) and
# wired in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Shop Management
# Moved to routers/shops.py (step 15 of the router/service split) and wired
# in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: User Management
# Moved to routers/users.py (step 16 of the router/service split) and wired
# in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Bill Management
# ══════════════════════════════════════════════════════════════════════════════

# Guards two threads *inside one process* — e.g. the nightly job firing while
# an admin presses "Generate rent bills". The MySQL named lock below guards
# across processes; this one costs nothing and closes the smaller gap.
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
            bill = Bill(
                user_id        = user.id,
                shop_id        = shop.id,
                bill_type      = "Rent",
                description    = "Auto-generated monthly rent",
                amount         = amount,
                paid_amount    = Decimal("0"),
                pending_amount = amount,
                bill_date      = target_dt,
                due_date       = target_dt,
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


# create_bill / list_bills / get_bill moved to routers/bills.py (step 17 of
# the router/service split) and wired in via app.include_router() near the
# top of this file.


# record_payment / auto_allocate_preview / auto_allocate_confirm / list_payments
# moved to routers/payments.py (step 18 of the router/service split) and wired
# in via app.include_router() near the top of this file.


# create_deposit_payment / list_deposit_payments moved to
# routers/deposit_payments.py (step 19 of the router/service split) and
# wired in via app.include_router() near the top of this file.


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Global Search
# Moved to routers/search.py (step 6a of the router/service split) and wired
# in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Reports + /api/finance/overview
# Moved to routers/reports.py (step 5 of the router/service split) and wired
# in via app.include_router() near the top of this file, alongside the
# audit-log router.
# ══════════════════════════════════════════════════════════════════════════════


# tenant_profile / tenant_shops / tenant_bills / tenant_payments moved to
# routers/tenant_portal.py (step 20 of the router/service split) and wired
# in via app.include_router() near the top of this file. _tenant_payment_dict
# moved to domain_helpers.py so both that router and tenant_home (below,
# which stays here) can share it.


# ────────────────────────────────────────────────────────────────────────
# Razorpay Standard Checkout - tenant pays online, either one bill (from
# the bill detail sheet) or the WHOLE pending balance in one go (from
# Home) - both go through the same two steps:
#
#   1. Tenant taps "Pay online" / "Pay bill" -> create-order (this decides
#      and locks in the amount server-side, and which bill(s) it can apply
#      to - a single bill, or every bill this tenant owes on).
#   2. Browser opens the Razorpay modal with that order_id.
#   3. On success, Razorpay hands the browser razorpay_payment_id/order_id/
#      signature -> verify (this is the ONLY place any Payment row gets
#      created from this flow; nothing is ever marked paid off a
#      signature-less client claim). One payment can become several
#      Payment rows if it's spread across bills - see
#      _allocate_razorpay_payment.
# ────────────────────────────────────────────────────────────────────────

def _razorpay_client(cfg: dict) -> "razorpay.Client":
    key_id, key_secret = _razorpay_credentials(cfg)
    if not key_id or not key_secret:
        # Distinct from "feature turned off" - this is a deployment gap the
        # admin needs to fix (Settings -> Online payments, or the server's
        # .env), not a business decision.
        raise HTTPException(503, detail="Online payments are not configured on this server.")
    return razorpay.Client(auth=(key_id, key_secret))


def _tenant_total_pending(db: Session, user_id: int) -> Decimal:
    """Sum of pending_amount across every unpaid/partial bill for this tenant."""
    total = sum(
        _decimal_to_float(b.pending_amount)
        for b in db.query(Bill).filter(Bill.user_id == user_id, Bill.status.in_(["pending", "partial"])).all()
    )
    return Decimal(str(total)).quantize(Decimal("0.01"))


def _allocate_razorpay_payment(
    db: Session, bills: list, amount: Decimal, razorpay_order_id: str, razorpay_payment_id: str, actor_id: int,
) -> list:
    """
    FIFO-allocate `amount` across `bills` (already row-locked, given in the
    order they should be paid first - oldest due date first for a
    whole-balance payment, or just the one bill for a single-bill payment),
    creating one Payment per bill that receives money and reconciling each
    via the same _reconcile_bill() every other payment path uses. Mirrors
    auto_allocate_confirm's algorithm exactly, but for one Razorpay-verified
    amount instead of admin-entered cash - fully automatic, no admin review
    step, since the signature already proved the money was actually paid.

    If the tenant's total pending balance shrank between create-order and
    verify (e.g. an admin recorded a manual payment in between), any
    leftover is applied on top of the last bill touched rather than being
    silently dropped - real money that was actually charged is never lost,
    even if that means a bill ends up briefly overpaid (the existing manual
    payment path already tolerates overpayment the same way).
    """
    remaining = amount
    payments = []
    now = datetime.now(timezone.utc)

    for bill in bills:
        if remaining <= 0:
            break
        outstanding = Decimal(str(_decimal_to_float(bill.pending_amount))).quantize(Decimal("0.01"))
        if outstanding <= 0:
            continue
        alloc = min(remaining, outstanding)

        pay = Payment(
            bill_id=bill.id, amount=alloc, payment_method="Razorpay",
            remarks=f"Razorpay payment {razorpay_payment_id}",
            payment_date=now,
            razorpay_order_id=razorpay_order_id, razorpay_payment_id=razorpay_payment_id,
        )
        db.add(pay)
        db.flush()
        db.refresh(bill)
        _reconcile_bill(bill)
        payments.append(pay)

        write_audit(db, actor_id, "CREATE", "payments", pay.id, new_data={
            "bill_id": bill.id, "amount": float(alloc), "payment_method": "Razorpay",
            "razorpay_order_id": razorpay_order_id, "razorpay_payment_id": razorpay_payment_id,
        })
        remaining -= alloc

    if remaining > 0 and payments:
        last = payments[-1]
        last.amount = Decimal(str(_decimal_to_float(last.amount))) + remaining
        db.flush()
        last_bill = db.query(Bill).filter(Bill.id == last.bill_id).first()
        db.refresh(last_bill)
        _reconcile_bill(last_bill)

    return payments


@app.post("/api/tenant/payments/razorpay/create-order",
          response_model=RazorpayCreateOrderResponse, tags=["Tenant"])
def create_razorpay_order(
    body:         RazorpayCreateOrderRequest,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """
    Step 1 of Razorpay Standard Checkout. The amount actually charged is
    decided HERE - never trusted from the client again - and stored in
    razorpay_orders so verify() has something authoritative to check
    against later.
    """
    cfg = settings_service.get_all(db)
    if not cfg.get("payment.razorpay_enabled"):
        raise HTTPException(403, detail="Online payments are currently turned off.")

    bill = None
    if body.bill_id is not None:
        bill = db.query(Bill).filter(Bill.id == body.bill_id).first()
        if not bill:
            raise HTTPException(404, detail="Bill not found")
        if bill.user_id != current_user.id:
            raise HTTPException(403, detail="This bill does not belong to you.")
        pending = Decimal(str(_decimal_to_float(bill.pending_amount))).quantize(Decimal("0.01"))
        if pending <= 0:
            raise HTTPException(400, detail="This bill is already fully paid.")
    else:
        pending = _tenant_total_pending(db, current_user.id)
        if pending <= 0:
            raise HTTPException(400, detail="You have no pending bills to pay.")

    if body.amount is None:
        charge = pending
    else:
        charge = Decimal(str(body.amount)).quantize(Decimal("0.01"))
        if charge <= 0:
            raise HTTPException(400, detail="Amount must be greater than zero.")
        if charge > pending:
            raise HTTPException(
                400, detail=f"Amount cannot exceed the pending balance of {float(pending)}.",
            )

    amount_paise = int(charge * 100)
    if amount_paise < 100:
        raise HTTPException(400, detail="Minimum payable amount is Rs 1 (100 paise).")

    client = _razorpay_client(cfg)
    try:
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"{'bill-' + str(bill.id) if bill else 'balance-' + str(current_user.id)}-"
                       f"{int(datetime.now(timezone.utc).timestamp())}",
            "notes": {
                "bill_id": str(bill.id) if bill else "ALL",
                "user_id": str(current_user.id),
            },
        })
    except razorpay.errors.BadRequestError as exc:
        msg = str(exc)
        if "auth" in msg.lower() or "key" in msg.lower():
            raise HTTPException(401, detail="Payment gateway authentication failed. Contact the office.")
        raise HTTPException(400, detail=f"Payment gateway rejected the request: {msg}")
    except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as exc:
        logger.error("Razorpay order creation failed for user %s: %s", current_user.id, exc)
        raise HTTPException(500, detail="Could not reach the payment gateway. Please try again.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Unexpected error creating Razorpay order for user %s: %s", current_user.id, exc)
        raise HTTPException(500, detail="Could not start the payment. Please try again.")

    order_row = RazorpayOrder(
        razorpay_order_id=order["id"], bill_id=(bill.id if bill else None), user_id=current_user.id,
        amount=charge, currency="INR", status="created",
    )
    db.add(order_row)
    db.flush()
    write_audit(db, current_user.id, "CREATE", "razorpay_orders", order_row.id, new_data={
        "bill_id": bill.id if bill else None, "amount": float(charge), "razorpay_order_id": order["id"],
    })
    db.commit()

    key_id, _ = _razorpay_credentials(cfg)
    return RazorpayCreateOrderResponse(
        order_id=order["id"], amount=amount_paise, currency="INR",
        key_id=key_id, bill_id=(bill.id if bill else None),
    )


class _RazorpayNoPendingBillsError(Exception):
    """Raised when a Razorpay order was proven paid but there is nothing left to apply it to."""


def _finalize_paid_razorpay_order(
    db: Session, order_row: "RazorpayOrder", razorpay_payment_id: str, actor_id: int,
) -> list:
    """
    Shared core of "this order has now been proven paid - actually allocate
    the money to bills and flip the order to 'paid'". Two independent paths
    can reach here for the same order: the tenant's own browser calling
    verify() right after checkout (HMAC-verified via the Key Secret), or
    Razorpay's server-to-server webhook (HMAC-verified via the Webhook
    Secret) reporting the same payment.captured event. Whichever gets here
    first while order_row.status is still "created" wins; callers must check
    that status themselves before calling this, and the caller that loses
    the race simply never gets a "created" order to act on - so the same
    payment can never be recorded twice no matter which path arrives first
    or how many times either one retries.

    Raises _RazorpayNoPendingBillsError if there's nowhere to apply the money
    (e.g. every bill was settled some other way in between) - callers decide
    how to surface that, since a webhook has no user to show an error to but
    verify() does.
    """
    if order_row.bill_id is not None:
        bills = db.query(Bill).filter(Bill.id == order_row.bill_id).with_for_update().all()
    else:
        bills = (
            db.query(Bill)
            .filter(Bill.user_id == order_row.user_id, Bill.status.in_(["pending", "partial"]))
            .order_by(Bill.due_date.is_(None), Bill.due_date.asc(), Bill.bill_date.asc(), Bill.id.asc())
            .with_for_update()
            .all()
        )

    if not bills:
        raise _RazorpayNoPendingBillsError("no pending bills to apply this payment to")

    payments = _allocate_razorpay_payment(
        db, bills, order_row.amount, order_row.razorpay_order_id, razorpay_payment_id, actor_id,
    )
    if not payments:
        raise RuntimeError("allocation produced no payments")

    order_row.status = "paid"
    order_row.payment_id = payments[0].id
    db.commit()
    for p in payments:
        db.refresh(p)
    return payments


@app.post("/api/tenant/payments/razorpay/verify", tags=["Tenant"])
def verify_razorpay_payment(
    body:         RazorpayVerifyRequest,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """
    Step 3 of Razorpay Standard Checkout. Verifies the HMAC-SHA256 signature
    (order_id + "|" + payment_id, signed with KEY_SECRET) via the SDK's own
    utility - Payment rows are only ever created after this succeeds. Bills
    are row-locked for the write so a duplicated/retried verify call can't
    double-record the same order.

    This only runs on the tenant's own device, so it's skipped entirely if
    their browser closes or loses network right after paying - see the
    /api/webhooks/razorpay route below, which independently catches that
    case from Razorpay's side.
    """
    order_row = (
        db.query(RazorpayOrder)
        .filter(RazorpayOrder.razorpay_order_id == body.razorpay_order_id)
        .first()
    )
    if not order_row:
        raise HTTPException(404, detail="Order not found.")
    if order_row.user_id != current_user.id:
        raise HTTPException(403, detail="This order does not belong to you.")
    if order_row.status != "created":
        # Already verified (or already failed) - never process the same order twice.
        # This is also what happens if the webhook already recorded this
        # exact payment before the tenant's browser got back to us.
        raise HTTPException(409, detail="This payment has already been processed.")

    cfg = settings_service.get_all(db)
    client = _razorpay_client(cfg)
    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id":   body.razorpay_order_id,
            "razorpay_payment_id": body.razorpay_payment_id,
            "razorpay_signature":  body.razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        order_row.status = "failed"
        db.commit()
        raise HTTPException(
            400, detail="Payment could not be verified. If money was deducted, contact the office.",
        )

    try:
        payments = _finalize_paid_razorpay_order(db, order_row, body.razorpay_payment_id, current_user.id)
    except _RazorpayNoPendingBillsError:
        # Verified money with nowhere to apply it (e.g. every bill was
        # settled some other way between create-order and verify) - never
        # silently drop it. Leave the order "created" so it isn't wasted;
        # the office reconciles it manually with the payment ID.
        db.rollback()
        raise HTTPException(
            500,
            detail="Payment was verified but there were no pending bills to apply it to. Contact "
                   f"the office with your payment ID ({body.razorpay_payment_id}).",
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            "Verified Razorpay payment %s (order %s) could not be recorded: %s",
            body.razorpay_payment_id, body.razorpay_order_id, exc,
        )
        raise HTTPException(
            500,
            detail="Payment was verified but could not be recorded. Contact the office with your "
                   f"payment ID ({body.razorpay_payment_id}) so it can be applied manually.",
        )

    touched_bill_ids = [p.bill_id for p in payments]
    touched_bills = db.query(Bill).filter(Bill.id.in_(touched_bill_ids)).all()
    bills_by_id = {b.id: b for b in touched_bills}

    return {
        "success": True,
        "message": "Payment verified and recorded.",
        "payments": [PaymentResponse.model_validate(p) for p in payments],
        "bills": [
            {
                "id": b.id, "status": b.status,
                "paid_amount": _decimal_to_float(b.paid_amount),
                "pending_amount": _decimal_to_float(b.pending_amount),
            }
            for b in (bills_by_id[bid] for bid in dict.fromkeys(touched_bill_ids))
        ],
    }


@app.post("/api/webhooks/razorpay", tags=["Tenant"])
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Server-to-server webhook (configure this URL on the Razorpay dashboard
    under Settings -> Webhooks, with events payment.captured and
    payment.failed checked). This exists alongside verify_razorpay_payment()
    above, not instead of it - the two cover different failure modes:

      - verify() runs on the tenant's own device right after checkout, so it
        can show them a result immediately, but only fires if their browser
        is still open and online when Razorpay's checkout callback returns.
      - This webhook comes from Razorpay's own servers, independent of the
        tenant's device, so it also catches payments that were captured
        successfully but where the browser was closed, the app crashed, or
        the network dropped before verify() could run - money Razorpay
        actually holds that would otherwise sit unrecorded until an admin
        manually reconciled it with the payment ID.

    Whichever of the two reaches a given order first "wins" (flips it from
    "created" to "paid" via the shared _finalize_paid_razorpay_order); the
    other finds the order already handled and is a no-op. That, plus Razorpay
    retrying this webhook on anything but a 2xx response, is why every branch
    below ends by returning 200 once the request's signature has checked out
    - there is no tenant on the other end of this call to show an error to,
    and repeated retries for something already handled (or already logged
    for manual follow-up) would only add noise.

    Unlike every other route in this file, there is deliberately no auth
    dependency here - Razorpay can't send a bearer token. Trust instead comes
    entirely from the HMAC-SHA256 signature Razorpay computes over the exact
    raw request body using the webhook secret (Settings -> Online payments ->
    Razorpay Webhook Secret), verified below BEFORE the body is parsed or
    trusted for anything else.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    cfg = settings_service.get_all(db)
    webhook_secret = _razorpay_webhook_secret(cfg)
    if not webhook_secret:
        logger.error("Razorpay webhook received but no webhook secret is configured on this server.")
        raise HTTPException(503, detail="Webhook is not configured on this server.")

    client = _razorpay_client(cfg)
    try:
        client.utility.verify_webhook_signature(raw_body.decode("utf-8"), signature, webhook_secret)
    except razorpay.errors.SignatureVerificationError:
        logger.warning("Razorpay webhook signature verification failed - rejecting request.")
        raise HTTPException(400, detail="Invalid webhook signature.")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(400, detail="Malformed webhook payload.")

    event = payload.get("event", "")

    if event == "payment.captured":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        razorpay_payment_id = payment_entity.get("id")
        if not razorpay_order_id or not razorpay_payment_id:
            logger.warning("Razorpay webhook payment.captured missing order_id/payment_id: %s", payload)
            return {"success": True}

        order_row = (
            db.query(RazorpayOrder)
            .filter(RazorpayOrder.razorpay_order_id == razorpay_order_id)
            .with_for_update()
            .first()
        )
        if not order_row:
            # Most likely: an order created by some other integration on the
            # same Razorpay account, or a stale test event - not this app's
            # concern, but still a validly-signed request, so ack it.
            logger.warning(
                "Razorpay webhook payment.captured for unknown order %s (payment %s)",
                razorpay_order_id, razorpay_payment_id,
            )
            return {"success": True}

        if order_row.status != "created":
            # Already recorded - either verify() beat us to it, or Razorpay
            # is retrying a webhook delivery we already handled. Normal, not
            # an error.
            return {"success": True}

        try:
            _finalize_paid_razorpay_order(db, order_row, razorpay_payment_id, order_row.user_id)
            logger.info(
                "Razorpay webhook recorded payment %s for order %s", razorpay_payment_id, razorpay_order_id,
            )
        except _RazorpayNoPendingBillsError:
            db.rollback()
            logger.warning(
                "Razorpay webhook: payment %s (order %s) captured but no pending bills to apply it "
                "to - left as 'created' for manual reconciliation.", razorpay_payment_id, razorpay_order_id,
            )
        except Exception as exc:
            db.rollback()
            logger.error(
                "Razorpay webhook: payment %s (order %s) could not be recorded: %s",
                razorpay_payment_id, razorpay_order_id, exc,
            )

    elif event == "payment.failed":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        if razorpay_order_id:
            order_row = (
                db.query(RazorpayOrder)
                .filter(RazorpayOrder.razorpay_order_id == razorpay_order_id)
                .first()
            )
            if order_row and order_row.status == "created":
                order_row.status = "failed"
                db.commit()

    # Every other subscribed event (order.paid, refund.*, etc.) is
    # acknowledged but otherwise ignored - nothing in this app reads them
    # today. Always 2xx for a validly-signed request so Razorpay doesn't
    # keep retrying indefinitely.
    return {"success": True}


# tenant_deposit_payments / tenant_financial_summary moved to
# routers/tenant_portal.py (step 20 of the router/service split) and wired
# in via app.include_router() near the top of this file.


# ========================================================================
# AUDIT LOG MODULE  +  BILL / PAYMENT EDIT APIs
# (Full code unchanged - no modifications needed)
# ========================================================================

# BillUpdate / PaymentUpdate / DepositPaymentUpdate now live in schemas.py
# (imported in the PYDANTIC SCHEMAS block near the top of this file).

# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Audit Log  (Admin only)
# Moved to routers/audit_log.py (step 3 of the router/service split) and
# wired in via app.include_router() near the bottom of this file, right
# after `app = FastAPI(...)` is constructed. _parse_json_field/
# _audit_log_to_dict moved with it - nothing outside this block ever called
# them.
# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Bill Edit  (Admin only)
# ══════════════════════════════════════════════════════════════════════════════

# update_bill / delete_bill moved to routers/bills.py (step 17 of the
# router/service split).


# get_payment / update_payment / delete_payment moved to routers/payments.py
# (step 18 of the router/service split) and wired in via app.include_router()
# near the top of this file.


# get_deposit_payment / update_deposit_payment / delete_deposit_payment
# moved to routers/deposit_payments.py (step 19 of the router/service split)
# and wired in via app.include_router() near the top of this file.


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: Admin Dashboard KPIs  (extra – helps frontend avoid 5 round-trips)
# Moved to routers/dashboard.py (step 6b of the router/service split) and
# wired in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: Bills with full context  (paginated list for admin table)
# Moved to routers/bills.py (step 17 of the router/service split).
# ══════════════════════════════════════════════════════════════════════════════


# list_payments_paginated moved to routers/payments.py (step 18 of the
# router/service split) and wired in via app.include_router() near the top
# of this file.


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

#=======================================================

# /api/ledger/monthly, /api/tenant/ledger/monthly, and _get_monthly_ledger_data
# moved to routers/ledger.py (step 7 of the router/service split) and wired
# in via app.include_router() near the top of this file.


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# SUBMETER READINGS
#
# Workflow (see meter_service.py for the rules):
#     tenant photographs the meter and types the reading   -> status "pending"
#     admin opens the SAME photo, types what they can see
#     admin approves  -> that admin value becomes the billed reading, and one
#                        Electricity bill is raised at the tariff live that day
#     admin rejects   -> reason recorded, no bill, tenant can resubmit
#
# The tenant's number and photo are evidence. The admin's verified reading is
# the only thing a bill is ever calculated from.
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════

# ── Schemas ───────────────────────────────────
# MeterCreate / MeterUpdate / TariffCreate / VerifyReadingRequest /
# ApproveReadingRequest / RejectReadingRequest / AssignMeterShopRequest /
# SettingsUpdateRequest now live in schemas.py (imported in the PYDANTIC
# SCHEMAS block near the top of this file).

# ── Helpers ───────────────────────────────────
# _meter_error / _tenant_shop_ids / _reading_to_dict moved to meter_helpers.py
# (step 9 of the router/service split) - imported below.

from meter_helpers import _meter_error, _tenant_shop_ids, _reading_to_dict


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Meters (admin)
# Moved to routers/meters.py (step 10 of the router/service split) and wired
# in via app.include_router() near the top of this file. _meter_to_dict moved
# with it - nothing outside this block ever called it.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Tariffs (admin)
# Moved to routers/meter_tariffs.py (step 8 of the router/service split) and
# wired in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Meter readings (tenant)
# Moved to routers/tenant_meters.py (step 11 of the router/service split) and
# wired in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: Tenant home bundle
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/tenant/home", tags=["Tenant"])
def tenant_home(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """
    Everything the tenant portal needs on open, in one response.

    The portal previously made seven parallel calls on every load. On a shop's
    phone connection each round trip costs more than the query does, so this
    collapses them into one. Same data, same shapes as the individual
    endpoints - those are all still there and still work, so nothing that
    already calls them breaks.
    """
    shop_ids = _tenant_shop_ids(db, current_user.id)

    # ── Shops (with agreement dates and rent) ──
    shops = []
    if shop_ids:
        rows = (
            db.query(Shop, UserShop)
            .join(UserShop, UserShop.shop_id == Shop.id)
            .filter(UserShop.user_id == current_user.id)
            .all()
        )
        complexes = {c.id: c.name for c in db.query(Complex).all()}
        shops = [
            {
                "id": s.id,
                "shop_number": s.shop_number,
                "complex_id": s.complex_id,
                "complex_name": complexes.get(s.complex_id),
                "area_sqft": _decimal_to_float(s.area_sqft),
                "shop_rent": _decimal_to_float(s.shop_rent),
                "shop_deposit": _decimal_to_float(s.shop_deposit),
                "agreement_start_date": us.agreement_start_date,
                "agreement_end_date": us.agreement_end_date,
            }
            for s, us in rows
        ]

    # ── Bills ──
    bills = db.query(Bill).filter(Bill.user_id == current_user.id).order_by(Bill.id).all()
    bills_out = [
        {
            "id": b.id, "shop_id": b.shop_id, "bill_type": b.bill_type,
            "description": b.description,
            "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "bill_date": b.bill_date, "due_date": b.due_date, "status": b.status,
        }
        for b in bills
    ]

    # ── Payments ──
    payments = (
        db.query(Payment)
        .join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == current_user.id)
        .order_by(Payment.id)
        .all()
    )

    # ── Deposit payments ──
    deposits = (
        db.query(DepositPayment)
        .filter(DepositPayment.user_id == current_user.id)
        .order_by(DepositPayment.payment_date.desc())
        .all()
    )

    # ── Meters + readings ──
    meters_out, readings_out = [], []
    if shop_ids:
        meters = (
            db.query(Meter)
            .filter(Meter.shop_id.in_(shop_ids), Meter.is_active == True)
            .order_by(Meter.id)
            .all()
        )
        shop_numbers = {s["id"]: s["shop_number"] for s in shops}
        for m in meters:
            pending = (
                db.query(MeterReading)
                .filter(
                    MeterReading.meter_id == m.id,
                    MeterReading.user_id == current_user.id,
                    MeterReading.status == "pending",
                )
                .order_by(MeterReading.id.desc())
                .first()
            )
            meters_out.append({
                "id": m.id,
                "meter_number": m.meter_number,
                "meter_type": m.meter_type,
                "shop_id": m.shop_id,
                "shop_number": shop_numbers.get(m.shop_id),
                "previous_reading": float(meter_service.previous_reading_value(db, m)),
                "pending_reading_id": pending.id if pending else None,
                "has_pending": pending is not None,
            })

        readings = (
            db.query(MeterReading)
            .filter(MeterReading.user_id == current_user.id)
            .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
            .limit(24)          # the portal only ever shows the recent ones
            .all()
        )
        readings_out = [_reading_to_dict(db, r) for r in readings]

    # ── Branding / payment-methods line ──
    cfg = settings_service.get_all(db)
    razorpay_enabled, razorpay_key_id = _razorpay_public_config(cfg)

    return {
        "profile": {
            "id": current_user.id, "name": current_user.name,
            "mobile": current_user.mobile, "email": current_user.email,
            "role": current_user.role,
        },
        "shops": shops,
        "bills": bills_out,
        "payments": [_tenant_payment_dict(p) for p in payments],
        "deposits": [
            {
                "id": dp.id, "shop_id": dp.shop_id,
                "amount": _decimal_to_float(dp.amount),
                "payment_date": dp.payment_date, "remarks": dp.remarks,
            }
            for dp in deposits
        ],
        "meters": meters_out,
        "readings": readings_out,
        "settings": {
            "app_name": cfg.get("app.name"),
            "currency_symbol": cfg.get("app.currency_symbol"),
            "support_contact": cfg.get("app.support_contact"),
            "payment_methods": cfg.get("app.payment_methods"),
            "labels": {
                "tenant": cfg.get("label.tenant_singular"),
                "shop": cfg.get("label.shop_singular"),
                "complex": cfg.get("label.complex_singular"),
            },
            "razorpay_enabled": razorpay_enabled,
            "razorpay_key_id": razorpay_key_id,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: Meter photo (tenant owner or admin only)
# ── ROUTES: Meter readings (admin review)
# Both moved to routers/meter_readings.py (step 12 of the router/service
# split) and wired in via app.include_router() near the top of this file.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Application settings
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/settings/public", tags=["Settings"])
def public_settings(db: Session = Depends(get_db)):
    """
    The handful of settings the sign-in page and both portals need before a
    user is authenticated (app name, tagline, currency symbol, support
    contact). Deliberately a small allow-list - no configuration that isn't
    already visible on screen is exposed here.
    """
    cfg = settings_service.get_all(db)
    razorpay_enabled, razorpay_key_id = _razorpay_public_config(cfg)
    return {
        "app_name": cfg.get("app.name"),
        "tagline": cfg.get("app.tagline"),
        "currency_symbol": cfg.get("app.currency_symbol"),
        "support_contact": cfg.get("app.support_contact"),
        "labels": {
            "tenant": cfg.get("label.tenant_singular"),
            "shop": cfg.get("label.shop_singular"),
            "complex": cfg.get("label.complex_singular"),
        },
        "razorpay_enabled": razorpay_enabled,
        "razorpay_key_id": razorpay_key_id,
    }


# GET /api/settings, PUT /api/settings, POST /api/settings/reset moved to
# routers/settings.py (step 13 of the router/service split) and wired in via
# app.include_router() near the top of this file. GET /api/settings/public
# stays here (see routers/settings.py's module docstring for why).


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint (for direct `python app.py` execution)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)