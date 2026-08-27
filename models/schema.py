"""
models/schema.py - Database Initialisation Script
Tenant Management System

Run once (or repeatedly – it is idempotent):
    python models/schema.py

What it does:
    1. Verifies the MySQL connection
    2. Creates ANY missing tables (brand-new models that don't exist yet in the DB)
    3. Adds ANY missing columns on tables that already exist but are missing
       fields defined on the ORM model (self-healing schema – safe to run repeatedly)
    4. Relaxes any column that used to be NOT NULL but whose model has since
       been changed to nullable (e.g. to support a new optional use case) -
       never the other direction, so this can't break existing rows
    5. Adds indexes and foreign keys (handled by SQLAlchemy metadata, best-effort
       for ones added after the table already exists)
    6. Inserts the default admin user (mobile: 8177809890)
    7. Prints a summary to stdout

Self-healing behaviour:
    Every time this script runs, it diffs Base.metadata against the live
    database schema:
      - A model with no matching table  -> CREATE TABLE (via create_all)
      - A model whose table exists but is missing one or more columns
        defined on the model -> ALTER TABLE ... ADD COLUMN for each missing
        column individually
      - A model whose table has a column that is NOT NULL in the database
        but nullable=True on the model -> ALTER TABLE ... MODIFY COLUMN ...
        NULL (one-way only: never tightens NULL -> NOT NULL automatically)
    This means if you add a new Column(...) to any model below, add an
    entirely new model class, or loosen an existing column's nullability,
    simply re-running `python models/schema.py` brings the live database up
    to date without any manual migration.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy import bindparam, or_

import bcrypt
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime, Date,
    Boolean, ForeignKey, Enum, Index, UniqueConstraint, inspect as sa_inspect,
    text,
)
from sqlalchemy.orm import relationship

from core.database import Base, engine, SessionLocal, test_connection
from core.logger import get_logger

logger = get_logger("app")

# ══════════════════════════════════════════════════════════════════════════════
# ORM MODELS
# These are imported by app.py as well – define them here so models/schema.py
# is the single source of truth for schema.
# ══════════════════════════════════════════════════════════════════════════════


def now_utc():
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────
# users
# ──────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    name          = Column(String(120), nullable=False)
    mobile        = Column(String(15),  nullable=False, unique=True, index=True)
    email         = Column(String(150), nullable=True,  unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role          = Column(Enum("admin", "tenant", name="user_role"), nullable=False, default="tenant")
    # Day-of-month (1-28) on which this user's monthly rent bill is raised.
    # Capped at 28 so it's valid in every month, including February.
    # Range is enforced at the API layer (see UserCreate/UserUpdate in app.py).
    rent_bill_date = Column(Integer, nullable=True)
    # Admin toggle: when True, the automatic rent-bill scheduler generates a
    # Rent bill for this user on rent_bill_date every month (only for shops
    # currently assigned to them). Off by default - opt-in per user.
    auto_rent_bill_enabled = Column(Boolean, nullable=False, default=False)
    is_active     = Column(Boolean, nullable=False, default=True)
    created_at    = Column(DateTime, nullable=False, default=now_utc)
    updated_at    = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    user_shops       = relationship("UserShop",       back_populates="user", cascade="all, delete-orphan")
    bills            = relationship("Bill",           back_populates="user")
    audit_logs       = relationship("AuditLog",        back_populates="user")
    deposit_payments = relationship("DepositPayment", back_populates="user")


# ──────────────────────────────────────────────
# complexes
# ──────────────────────────────────────────────
class Complex(Base):
    __tablename__ = "complexes"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(150), nullable=False)
    address     = Column(Text,        nullable=True)
    description = Column(Text,        nullable=True)
    created_at  = Column(DateTime, nullable=False, default=now_utc)
    updated_at  = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    shops = relationship("Shop", back_populates="complex")


# ──────────────────────────────────────────────
# shops
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# shops
# ──────────────────────────────────────────────
class Shop(Base):
    __tablename__ = "shops"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    shop_number  = Column(String(50), nullable=False)
    area_sqft    = Column(Numeric(10, 2), nullable=True)
    status       = Column(Enum("available", "occupied", "maintenance", name="shop_status"),
                          nullable=False, default="available")
    complex_id   = Column(Integer, ForeignKey("complexes.id", ondelete="SET NULL"), nullable=True, index=True)
    # Current monthly rent for this shop – this is the single source of truth
    # for rent billing. All future Rent bills are created using this value.
    shop_rent    = Column(Numeric(10, 2), nullable=False, default=0)
    shop_deposit = Column(Numeric(10, 2), nullable=False, default=0)
    created_at   = Column(DateTime, nullable=False, default=now_utc)
    updated_at   = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    complex          = relationship("Complex",        back_populates="shops")
    user_shops        = relationship("UserShop",       back_populates="shop", cascade="all, delete-orphan")
    bills             = relationship("Bill",           back_populates="shop")
    deposit_payments  = relationship("DepositPayment", back_populates="shop")
    meters            = relationship("Meter",          back_populates="shop", cascade="all, delete-orphan")


# ──────────────────────────────────────────────
# user_shops  (many-to-many junction)
# ──────────────────────────────────────────────
class UserShop(Base):
    __tablename__ = "user_shops"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id     = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    assigned_at = Column(DateTime, nullable=False, default=now_utc)
    agreement_start_date = Column(DateTime, nullable=True)
    agreement_end_date = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="user_shops")
    shop = relationship("Shop", back_populates="user_shops")

    # A shop can be assigned to a user only once
    __table_args__ = (
        Index("uq_user_shop", "user_id", "shop_id", unique=True),
    )


# ──────────────────────────────────────────────
# bills
# ──────────────────────────────────────────────
class Bill(Base):
    __tablename__ = "bills"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    shop_id         = Column(Integer, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False, index=True)
    bill_type       = Column(String(80),  nullable=False)
    description     = Column(Text,        nullable=True)
    amount          = Column(Numeric(12, 2), nullable=False)
    paid_amount     = Column(Numeric(12, 2), nullable=False, default=0)
    pending_amount  = Column(Numeric(12, 2), nullable=False, default=0)
    bill_date       = Column(DateTime, nullable=False, default=now_utc)
    due_date        = Column(DateTime, nullable=True)
    status          = Column(Enum("pending", "partial", "paid", name="bill_status"),
                             nullable=False, default="pending")
    created_at      = Column(DateTime, nullable=False, default=now_utc)

    # ── Late-payment penalty ──────────────────────────────────────────────
    # `amount` above is and stays the ORIGINAL bill: penalties never touch it,
    # so "what was this bill for" is always answerable however long it has been
    # overdue. What is owed = amount + penalty_amount, and that sum is what
    # pending_amount holds (see domain_helpers._reconcile_bill, the one place
    # that computes it).
    penalty_amount  = Column(Numeric(12, 2), nullable=False, default=0)
    # Chargeable days actually billed for - days past the due date MINUS the
    # grace period, not the raw overdue count.
    penalty_days    = Column(Integer, nullable=False, default=0)
    # The last date the penalty has been calculated up to. Purely informational:
    # the penalty is recomputed from scratch on every run rather than
    # incremented, which is what makes running the task twice in a day a no-op.
    penalty_charged_through = Column(Date, nullable=True)

    # ── Duplicate rent protection ─────────────────────────────────────────
    # "RENT-2026-09" for a Rent bill; NULL for every other bill type. With the
    # unique index below, a second Rent bill for the same tenant and shop in
    # the same month is impossible AT THE DATABASE, not merely unlikely -
    # which is what stops the 2026-08-13 double-billing from ever recurring,
    # whichever process or future code path tries it.
    #
    # NULL for non-Rent bills is deliberate: MySQL and SQLite both allow
    # repeated NULLs in a unique index, so utility bills, deposits and manual
    # charges are unconstrained while Rent is not.
    #
    # Set by scheduler/auto_rent_generation/auto_rent_generation.py and by the
    # admin's manual bill creation (routers/bills.py) - the two places a Rent
    # bill can come into existence.
    rent_period = Column(String(20), nullable=True, index=True)

    # ── Late fee as its own bill ──────────────────────────────────────────
    # Set on a Penalty bill, pointing at the bill the fee is for. NULL on
    # every ordinary bill.
    #
    # A late fee used to live in penalty_amount above, which meant one row
    # carried two different kinds of money: a "Rent" bill for 10,000 would
    # display 23,200 to pay. Nobody could tell which figure was rent and which
    # was the fee, a payment could not be attributed to one or the other, and
    # rent income could not be reported apart from penalty income. Splitting
    # the fee into its own bill makes a rent bill mean rent again.
    #
    # ONE fee bill per parent, not one per day - a bill 132 days overdue would
    # otherwise spawn 132 rows. The single fee bill's amount is recomputed
    # while it is unpaid. The unique index below is what enforces that.
    # No index=True: the UNIQUE index below already covers lookups on this
    # column, and a second index on the same column is dead weight on every
    # write.
    parent_bill_id = Column(Integer, ForeignKey("bills.id", ondelete="CASCADE"),
                            nullable=True)

    # Relationships
    user     = relationship("User",    back_populates="bills")
    shop     = relationship("Shop",    back_populates="bills")
    payments = relationship("Payment", back_populates="bill", cascade="all, delete-orphan")

    # The fee raised against this bill, if any. Deleting a bill takes its fee
    # with it: a late fee for a bill that no longer exists is not collectable.
    late_fee_bill = relationship(
        "Bill", remote_side=[id], backref="fee_bills", uselist=False,
        foreign_keys=[parent_bill_id],
    )

    __table_args__ = (
        Index("uq_bill_rent_period", "user_id", "shop_id", "rent_period", unique=True),
        # One fee bill per parent. Repeated NULLs are allowed, so ordinary
        # bills are unconstrained; a second fee bill for the same parent is
        # refused by the database rather than by whichever code path
        # remembered to check.
        Index("uq_bill_parent", "parent_bill_id", unique=True),
    )


# ──────────────────────────────────────────────
# payments
# ──────────────────────────────────────────────
class Payment(Base):
    __tablename__ = "payments"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    bill_id        = Column(Integer, ForeignKey("bills.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount         = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(60), nullable=False)
    payment_date   = Column(DateTime, nullable=False, default=now_utc)
    remarks        = Column(Text, nullable=True)
    created_at     = Column(DateTime, nullable=False, default=now_utc)

    # Set only when this payment came through Razorpay (payment_method =
    # "Razorpay") rather than being recorded manually by an admin. NULL for
    # every payment recorded the old way - nothing else changes for those.
    razorpay_order_id   = Column(String(64), nullable=True, index=True)
    razorpay_payment_id = Column(String(64), nullable=True, index=True)

    # Relationships
    bill = relationship("Bill", back_populates="payments")


# ──────────────────────────────────────────────
# razorpay_orders
# One row per order created for a tenant's "Pay online" click - NOT a
# payment record. Exists so the verify step can check the amount and
# ownership it itself decided at create-order time, rather than trusting
# whatever the browser echoes back, and so a given order can't be replayed
# into a second Payment if verify is somehow called twice.
# ──────────────────────────────────────────────
class RazorpayOrder(Base):
    __tablename__ = "razorpay_orders"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    razorpay_order_id = Column(String(64), nullable=False, unique=True, index=True)
    # NULL means "pay my whole pending balance" rather than one specific
    # bill - the amount then gets FIFO-allocated across every bill the
    # tenant owes on at verify time (see _allocate_razorpay_payment).
    bill_id          = Column(Integer, ForeignKey("bills.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount           = Column(Numeric(12, 2), nullable=False)   # rupees, matches Payment.amount
    currency         = Column(String(8), nullable=False, default="INR")
    status           = Column(Enum("created", "paid", "failed", name="razorpay_order_status"),
                              nullable=False, default="created", index=True)
    payment_id       = Column(Integer, ForeignKey("payments.id", ondelete="SET NULL"), nullable=True)
    created_at       = Column(DateTime, nullable=False, default=now_utc)
    updated_at       = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    bill = relationship("Bill")
    user = relationship("User")
    payment = relationship("Payment")


# ──────────────────────────────────────────────
# deposit_payments
# ──────────────────────────────────────────────
class DepositPayment(Base):
    __tablename__ = "deposit_payments"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    shop_id      = Column(Integer, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount       = Column(Numeric(10, 2), nullable=False)
    payment_date = Column(DateTime, nullable=False, default=now_utc)
    remarks      = Column(Text, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    user = relationship("User", back_populates="deposit_payments")
    shop = relationship("Shop", back_populates="deposit_payments")


# ──────────────────────────────────────────────
# meters  (electricity submeters attached to a shop)
# ──────────────────────────────────────────────
class Meter(Base):
    """
    A submeter. Normally installed at a shop - a shop may have more than one
    (e.g. separate light/power meters), so this is a one-to-many from Shop.

    shop_id is NULLABLE on purpose: a meter can be registered before it is
    fitted anywhere (a spare in stock, or one whose shop hasn't been created
    yet) and assigned to a shop later. An unassigned meter is invisible to
    tenants - nobody can submit a reading against it until it has a shop.

    initial_reading is the meter face value on the day it was installed. It is
    used as the "previous reading" for the very first reading submitted, so the
    first bill only charges units consumed since installation.
    """
    __tablename__ = "meters"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    shop_id           = Column(Integer, ForeignKey("shops.id", ondelete="SET NULL"), nullable=True, index=True)
    meter_number      = Column(String(60), nullable=False, index=True)
    meter_type        = Column(String(40), nullable=False, default="electricity")
    initial_reading   = Column(Numeric(12, 2), nullable=False, default=0)
    installation_date = Column(DateTime, nullable=True)
    notes             = Column(Text, nullable=True)
    is_active         = Column(Boolean, nullable=False, default=True)
    created_at        = Column(DateTime, nullable=False, default=now_utc)
    updated_at        = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    shop     = relationship("Shop", back_populates="meters")
    readings = relationship("MeterReading", back_populates="meter", cascade="all, delete-orphan")

    __table_args__ = (
        # The same physical meter number can't be registered twice on one shop.
        # Unassigned meters (shop_id NULL) are exempt - SQL treats NULLs as
        # distinct - so duplicates among those are checked in the API layer.
        Index("uq_meter_shop_number", "shop_id", "meter_number", unique=True),
    )


# ──────────────────────────────────────────────
# meter_tariffs  (unit price history)
# ──────────────────────────────────────────────
class MeterTariff(Base):
    """
    Price per unit, effective from a given date. Never edit history: to change
    the rate, add a new row with a later effective_from. The applicable tariff
    for a reading is the most recent row whose effective_from <= reading date,
    which keeps old bills reproducible at the rate that was live at the time.

    meter_type lets electricity and (future) water meters have separate rates.
    """
    __tablename__ = "meter_tariffs"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    meter_type     = Column(String(40), nullable=False, default="electricity", index=True)
    unit_price     = Column(Numeric(10, 4), nullable=False)
    fixed_charge   = Column(Numeric(10, 2), nullable=False, default=0)
    tax_percent    = Column(Numeric(5, 2),  nullable=False, default=0)
    effective_from = Column(DateTime, nullable=False, index=True)
    notes          = Column(Text, nullable=True)
    created_by     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at     = Column(DateTime, nullable=False, default=now_utc)


# ──────────────────────────────────────────────
# meter_readings
# ──────────────────────────────────────────────
class MeterReading(Base):
    """
    One submission in the submeter workflow. The full history of the reading is
    preserved on a single row so it can be audited later:

        customer_reading        - what the tenant typed in
        photo_path              - the ORIGINAL evidence photo, never overwritten
        admin_verified_reading  - what the admin read off that photo (authority)
        approved_reading        - copied from admin_verified_reading on approval

    The bill is only ever calculated from approved_reading. bill_id is UNIQUE,
    so the database itself guarantees an approved reading can produce at most
    one bill even if Approve is double-clicked or two admins race.
    """
    __tablename__ = "meter_readings"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    meter_id    = Column(Integer, ForeignKey("meters.id", ondelete="CASCADE"),  nullable=False, index=True)
    shop_id     = Column(Integer, ForeignKey("shops.id",  ondelete="RESTRICT"), nullable=False, index=True)
    # Whose meter this is - always the tenant, even when an admin submits the
    # reading on their behalf (see collected_by below). Everything that shows
    # "who this reading belongs to" (review queue, tenant's own history) reads
    # this column, never collected_by.
    user_id     = Column(Integer, ForeignKey("users.id",  ondelete="RESTRICT"), nullable=False, index=True)
    # Set only when an admin submitted this reading for the tenant (e.g. the
    # tenant can't use the app). NULL means the tenant submitted it themselves.
    collected_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Snapshot of the previous approved reading at submission time, so the
    # consumption maths stays reproducible even if later rows change.
    previous_reading = Column(Numeric(12, 2), nullable=False, default=0)

    # What the tenant entered (supporting information only - never billed from).
    customer_reading = Column(Numeric(12, 2), nullable=False)
    customer_note    = Column(Text, nullable=True)

    # The evidence photo (stored on disk, served only through an authorised
    # endpoint - the path is never exposed directly to the browser).
    photo_path         = Column(String(400), nullable=True)
    photo_original_name = Column(String(255), nullable=True)
    photo_size_bytes   = Column(Integer, nullable=True)
    photo_mime         = Column(String(80), nullable=True)

    # What the admin read off the photo. THIS is the billing authority.
    admin_verified_reading = Column(Numeric(12, 2), nullable=True)
    admin_verified_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    admin_verified_at      = Column(DateTime, nullable=True)
    admin_note             = Column(Text, nullable=True)
    # Filled in when the admin's reading differs from the customer's.
    override_reason        = Column(Text, nullable=True)

    approved_reading = Column(Numeric(12, 2), nullable=True)
    approved_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at      = Column(DateTime, nullable=True)

    reading_date     = Column(DateTime, nullable=False, default=now_utc, index=True)
    calculated_units = Column(Numeric(12, 2), nullable=True)

    # Tariff actually applied, copied onto the row so a later price change
    # never rewrites the history of an already-approved reading.
    unit_price_applied = Column(Numeric(10, 4), nullable=True)
    tariff_id          = Column(Integer, ForeignKey("meter_tariffs.id", ondelete="SET NULL"), nullable=True)

    status = Column(
        Enum("pending", "approved", "rejected", name="meter_reading_status"),
        nullable=False, default="pending", index=True,
    )
    rejection_reason = Column(Text, nullable=True)
    rejected_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rejected_at      = Column(DateTime, nullable=True)

    # UNIQUE - the database-level guarantee of "one approved reading -> one bill".
    bill_id = Column(Integer, ForeignKey("bills.id", ondelete="SET NULL"), nullable=True, unique=True)

    created_at = Column(DateTime, nullable=False, default=now_utc)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    meter = relationship("Meter", back_populates="readings")
    shop  = relationship("Shop")
    user  = relationship("User", foreign_keys=[user_id])
    bill  = relationship("Bill", foreign_keys=[bill_id])

    __table_args__ = (
        Index("ix_meter_readings_status_date", "status", "reading_date"),
        Index("ix_meter_readings_meter_status", "meter_id", "status"),
    )


# ──────────────────────────────────────────────
# app_settings  (runtime configuration, editable from the admin UI)
# ──────────────────────────────────────────────
class SchedulerRun(Base):
    """
    One row per execution of one scheduler script - the run log.

    Written by the standalone scripts under scheduler/, read by
    routers/scheduler_tracking.py. The application never writes here: the
    scripts talk to this database and nothing else, and the API only reads
    back what they recorded.

    `run_id` is the human-readable handle for one execution
    (AUTO_RENT-20260827-020015-3f9a). It is what the dashboard shows, what the
    per-item rows point at, and what someone quotes when asking why a bill
    looks the way it does - which is why it is a readable string rather than a
    bare UUID.

    `status` distinguishes three outcomes that matter operationally:
      SUCCESS  everything the run attempted worked
      PARTIAL  some items failed, the rest were still processed - the run did
               useful work and should NOT simply be re-run blindly
      FAILED   the run itself could not proceed (no database, bad config)
    """

    __tablename__ = "scheduler_runs"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    run_id    = Column(String(64), nullable=False, unique=True, index=True)
    # "auto_rent_generation" or "due_bill_penalty".
    scheduler = Column(String(40), nullable=False, index=True)
    # The business date the run covers. Usually today, but a catch-up run for
    # a tenant whose rent day was the 5th still carries the date it ran for.
    run_date  = Column(Date, nullable=False, index=True)

    started_at  = Column(DateTime, nullable=False, default=now_utc)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    status = Column(
        Enum("RUNNING", "SUCCESS", "PARTIAL", "FAILED", name="scheduler_run_status"),
        nullable=False, default="RUNNING", index=True,
    )
    # "cron" for a scheduled run, "manual" when someone ran the script by hand.
    trigger_source = Column(String(20), nullable=False, default="cron")

    items_total     = Column(Integer, nullable=False, default=0)
    items_succeeded = Column(Integer, nullable=False, default=0)
    items_failed    = Column(Integer, nullable=False, default=0)
    items_skipped   = Column(Integer, nullable=False, default=0)
    # Rent raised, or penalty added, depending on the scheduler.
    amount_total    = Column(Numeric(14, 2), nullable=False, default=0)

    error_message = Column(Text, nullable=True)
    hostname      = Column(String(120), nullable=True)

    created_at = Column(DateTime, nullable=False, default=now_utc)

    items = relationship("SchedulerRunItem", back_populates="run",
                         cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_scheduler_runs_sched_date", "scheduler", "run_date"),
        Index("ix_scheduler_runs_status_started", "status", "started_at"),
    )


class SchedulerRunItem(Base):
    """
    One row per customer/bill a scheduler run actually touched.

    This is what makes the tracking answerable rather than merely present:
    which tenant got rent on which day, which bill was skipped as a duplicate,
    how much penalty a bill received and - in `reason` - the arithmetic behind
    it as it stood that night.

    `user_name` and `shop_number` are COPIED here, not joined at read time. A
    tenant who moves out or a shop that is renumbered must not rewrite last
    year's billing history; the record of what happened has to stay readable
    after the rows it referred to have changed.

    `reason` is written by the script at the moment it decided, in the settings
    that applied then. Recomputing an explanation from today's settings would
    quietly disagree with the charge actually made.
    """

    __tablename__ = "scheduler_run_items"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    run_id    = Column(String(64), ForeignKey("scheduler_runs.run_id", ondelete="CASCADE"),
                       nullable=False, index=True)
    scheduler = Column(String(40), nullable=False, index=True)
    run_date  = Column(Date, nullable=False, index=True)

    # Who and where. IDs for filtering, names for reading back later.
    user_id     = Column(Integer, nullable=True, index=True)
    user_name   = Column(String(120), nullable=True)
    shop_id     = Column(Integer, nullable=True, index=True)
    shop_number = Column(String(50), nullable=True)
    bill_id     = Column(Integer, nullable=True, index=True)

    # RENT_CREATED / SKIPPED_DUPLICATE / SKIPPED_NO_SHOP / SKIPPED_ZERO_RENT /
    # PENALTY_APPLIED / PENALTY_UNCHANGED / PENALTY_REDUCED / FAILED
    action = Column(String(40), nullable=False, index=True)
    status = Column(
        Enum("SUCCESS", "SKIPPED", "FAILED", name="scheduler_item_status"),
        nullable=False, default="SUCCESS", index=True,
    )

    # Rent raised, or the penalty delta applied.
    amount = Column(Numeric(14, 2), nullable=True)

    # Penalty detail, as it applied on the night. Null for rent items.
    penalty_amount = Column(Numeric(14, 2), nullable=True)
    penalty_days   = Column(Integer, nullable=True)
    penalty_rate   = Column(Numeric(6, 3), nullable=True)
    bill_due_date  = Column(DateTime, nullable=True)

    # "RENT-2026-09" - the same value written to bills.rent_period, so a
    # duplicate is visible here even when it was correctly prevented there.
    period_key = Column(String(20), nullable=True, index=True)

    # The sentence the dashboard shows: why this happened, in figures.
    reason        = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=now_utc)

    run = relationship("SchedulerRun", back_populates="items")

    __table_args__ = (
        Index("ix_scheduler_items_sched_date", "scheduler", "run_date"),
        Index("ix_scheduler_items_user_date", "user_id", "run_date"),
        Index("ix_scheduler_items_action", "action", "run_date"),
    )


class AppSetting(Base):
    """
    Key/value application configuration so an admin can change branding, upload
    limits and billing behaviour from the frontend instead of redeploying.

    Defaults live in services/settings.py; a row here only exists once a value
    has actually been customised, so upgrading the defaults still works.
    """
    __tablename__ = "app_settings"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    key         = Column(String(120), nullable=False, unique=True, index=True)
    value       = Column(Text, nullable=True)
    updated_by  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at  = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)


# ──────────────────────────────────────────────
# audit_logs
# ──────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action     = Column(String(50),  nullable=False)          # CREATE / UPDATE / DELETE / LOGIN
    table_name = Column(String(80),  nullable=True)
    record_id  = Column(Integer,     nullable=True)
    old_data   = Column(Text,        nullable=True)           # JSON string
    new_data   = Column(Text,        nullable=True)           # JSON string
    created_at = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    user = relationship("User", back_populates="audit_logs")


# ══════════════════════════════════════════════════════════════════════════════
# HELPER – hash a plaintext password
# ══════════════════════════════════════════════════════════════════════════════

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# ══════════════════════════════════════════════════════════════════════════════
# SELF-HEALING SCHEMA SYNC
# Detects and fixes, on every run:
#   (a) entirely missing tables   -> CREATE TABLE
#   (b) missing columns on tables that DO exist -> ALTER TABLE ADD COLUMN
#   (c) a column that used to be NOT NULL but the model has since made
#       optional -> ALTER TABLE MODIFY COLUMN ... NULL (never the reverse -
#       tightening NULL -> NOT NULL is not attempted automatically, since it
#       could break rows that already contain NULL)
#   (d) missing indexes defined on the model     -> CREATE INDEX (best effort)
# Safe to run any number of times; every check is "if missing/mismatched, then heal".
# ══════════════════════════════════════════════════════════════════════════════

def _default_clause_for_column(column, dialect) -> str:
    """
    Build a safe SQL DEFAULT clause for an ALTER TABLE ADD COLUMN statement.
    Returns "" if there's no usable scalar default (caller then typically
    falls back to allowing NULL, or the column is required and the admin
    will need to backfill manually for pre-existing rows).
    """
    if column.default is None or not getattr(column.default, "is_scalar", False):
        return ""

    value = column.default.arg

    # Numeric-ish columns: no quotes
    if isinstance(value, (int, float, Decimal)):
        return f"DEFAULT {value}"
    # Booleans -> MySQL tinyint(1)
    if isinstance(value, bool):
        return f"DEFAULT {1 if value else 0}"
    # Callable defaults (e.g. now_utc) can't be expressed as a static SQL
    # DEFAULT for arbitrary dialects here — skip, NULL/NOT NULL governs instead.
    if callable(value):
        return ""
    # Fall back to a quoted string literal
    escaped = str(value).replace("'", "''")
    return f"DEFAULT '{escaped}'"


def sync_schema(connection) -> dict:
    """
    Compare Base.metadata against the live database and heal any gaps:
      - Missing tables  -> created via Base.metadata.create_all (checkfirst)
      - Missing columns on existing tables -> ALTER TABLE ADD COLUMN
      - Missing simple/unique indexes -> CREATE INDEX (best effort, ignored
        if the index engine-specific syntax doesn't apply)

    Returns a summary dict: {"tables_created": [...], "columns_added": [...],
    "columns_relaxed": [...], "indexes_added": [...], "errors": [...]}
    """
    summary = {"tables_created": [], "columns_added": [], "columns_relaxed": [], "indexes_added": [], "errors": []}

    inspector = sa_inspect(engine)
    existing_tables_before = set(inspector.get_table_names())

    # ── Step 1: create any tables that don't exist at all ──
    all_model_tables = set(Base.metadata.tables.keys())
    missing_tables = all_model_tables - existing_tables_before
    if missing_tables:
        Base.metadata.create_all(bind=engine, checkfirst=True)
        summary["tables_created"] = sorted(missing_tables)
        for t in sorted(missing_tables):
            print(f"  ✔  Created missing table: {t}")
            logger.info("Created missing table: %s", t)

    # Refresh inspector state after potential table creation
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # ── Step 2: for tables that exist (pre-existing or just created), check columns ──
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            # Should not happen post create_all, but guard anyway
            continue
        if table.name in missing_tables:
            # Brand new table already has every column from create_all
            continue

        existing_cols = {col["name"] for col in inspector.get_columns(table.name)}

        for column in table.columns:
            if column.name in existing_cols:
                continue  # column already present – nothing to heal

            col_type = column.type.compile(dialect=engine.dialect)
            default_clause = _default_clause_for_column(column, engine.dialect)

            # A NOT NULL column being added to a table that may already have
            # rows needs a default, otherwise the ALTER fails on most engines.
            # If the model declares NOT NULL but we couldn't derive a literal
            # default (e.g. callable like now_utc), relax to NULLable so the
            # migration doesn't break existing data; the app should backfill.
            if column.nullable:
                nullable_clause = "NULL"
            elif default_clause:
                nullable_clause = "NOT NULL"
            else:
                nullable_clause = "NULL"
                logger.warning(
                    "Column %s.%s is NOT NULL with no static default; "
                    "adding as NULLable to avoid breaking existing rows.",
                    table.name, column.name,
                )

            alter_sql = (
                f"ALTER TABLE `{table.name}` "
                f"ADD COLUMN `{column.name}` {col_type} {nullable_clause} {default_clause};"
            )
            try:
                connection.execute(text(alter_sql))
                connection.commit()
                summary["columns_added"].append(f"{table.name}.{column.name}")
                print(f"  ✔  Added missing column: {table.name}.{column.name}")
                logger.info("Added missing column: %s.%s", table.name, column.name)
            except Exception as exc:
                connection.rollback()
                summary["errors"].append(f"{table.name}.{column.name}: {exc}")
                logger.warning("Could not add column %s.%s: %s", table.name, column.name, exc)

        # ── Step 2b: relax NOT NULL -> NULL on columns that already existed
        # before the model loosened them (e.g. a column that used to always
        # be required, later changed to optional for a new use case). We only
        # ever go NOT NULL -> NULL here, never the reverse: a nullable column
        # can hold every value a NOT NULL one already does, so this direction
        # can never break existing rows. Tightening isn't attempted
        # automatically since existing NULLs would then violate the column.
        existing_col_info = {col["name"]: col for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if not column.nullable:
                continue  # model requires NOT NULL here - nothing to relax
            db_col = existing_col_info.get(column.name)
            if db_col is None or db_col.get("nullable", True):
                continue  # brand new column (handled above) or already nullable

            col_type = column.type.compile(dialect=engine.dialect)
            alter_sql = (
                f"ALTER TABLE `{table.name}` "
                f"MODIFY COLUMN `{column.name}` {col_type} NULL;"
            )
            try:
                connection.execute(text(alter_sql))
                connection.commit()
                summary["columns_relaxed"].append(f"{table.name}.{column.name}")
                print(f"  ✔  Relaxed NOT NULL -> NULL: {table.name}.{column.name}")
                logger.info("Relaxed NOT NULL -> NULL: %s.%s", table.name, column.name)
            except Exception as exc:
                connection.rollback()
                summary["errors"].append(f"{table.name}.{column.name} (relax nullable): {exc}")
                logger.warning("Could not relax nullable on %s.%s: %s", table.name, column.name, exc)

        # ── Step 3: best-effort check for missing simple/unique indexes ──
        try:
            # Keyed on (columns, is_unique), NOT columns alone. Matching on
            # columns only meant a UNIQUE index was silently skipped whenever a
            # plain index already existed on the same column - so a constraint
            # added later was never actually created, and the guarantee it was
            # there to provide quietly did not exist. A non-unique index does
            # not cover a unique one; a unique index does cover a plain one.
            existing_index_cols = set()
            for idx in inspector.get_indexes(table.name):
                cols = tuple(idx["column_names"])
                is_unique = bool(idx.get("unique"))
                existing_index_cols.add((cols, is_unique))
                if is_unique:
                    existing_index_cols.add((cols, False))

            # A primary key covers both.
            pk_cols = tuple(inspector.get_pk_constraint(table.name).get("constrained_columns") or [])
            if pk_cols:
                existing_index_cols.add((pk_cols, True))
                existing_index_cols.add((pk_cols, False))

            for index in table.indexes:
                idx_cols = tuple(col.name for col in index.columns)
                if (idx_cols, bool(index.unique)) in existing_index_cols:
                    continue
                # Skip if any column in the index didn't exist before this run
                # and failed to be added above
                if not all(c in {col["name"] for col in inspector.get_columns(table.name)} for c in idx_cols):
                    continue
                unique_kw = "UNIQUE " if index.unique else ""
                cols_sql = ", ".join(f"`{c}`" for c in idx_cols)
                create_idx_sql = f"CREATE {unique_kw}INDEX `{index.name}` ON `{table.name}` ({cols_sql});"
                try:
                    connection.execute(text(create_idx_sql))
                    connection.commit()
                    summary["indexes_added"].append(f"{table.name}:{index.name}")
                    print(f"  ✔  Added missing index: {table.name}.{index.name}")
                    logger.info("Added missing index: %s.%s", table.name, index.name)
                except Exception as exc:
                    connection.rollback()
                    summary["errors"].append(f"{table.name}:{index.name}: {exc}")
                    print(f"  \u26a0  Could not add index {table.name}.{index.name}")
                    if index.unique:
                        print(f"     The data already breaks this constraint. Find the "
                              f"duplicates with:\n"
                              f"       SELECT {', '.join(idx_cols)}, COUNT(*) FROM {table.name} "
                              f"GROUP BY {', '.join(idx_cols)} HAVING COUNT(*) > 1;")
                    logger.warning("Could not add index %s.%s: %s", table.name, index.name, exc)
        except Exception as exc:
            logger.warning("Index check skipped for %s: %s", table.name, exc)

    return summary


# Backwards-compatible alias (older scripts/imports may reference this name)
def add_missing_columns(connection):
    return sync_schema(connection)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def backfill_rent_periods() -> dict:
    """
    Fill bills.rent_period for Rent bills raised before the column existed.

    The value is derived, not invented: RENT-<year>-<month> of the bill date,
    which is exactly what the scheduler writes for new bills.

    Any tenant/shop that ALREADY has two Rent bills in the same month is left
    with rent_period NULL on the duplicates and reported here. That is
    deliberate. Filling them in would make the unique index impossible to
    create, and silently deleting a bill someone may have taken payment
    against is not this function's decision to make - so the duplicates are
    named, and the operator decides.

    Idempotent: rows that already have a value are not touched.
    """
    result = {"filled": 0, "duplicates": []}
    with engine.connect() as conn:
        try:
            # Group Rent bills by tenant/shop/month and find the ones with
            # more than one - those cannot all carry the same period key.
            rows = conn.execute(text(
                """
                SELECT user_id, shop_id,
                       YEAR(bill_date)  AS y,
                       MONTH(bill_date) AS m,
                       COUNT(*)         AS n,
                       GROUP_CONCAT(id) AS ids
                  FROM bills
                 WHERE bill_type = 'Rent'
                 GROUP BY user_id, shop_id, YEAR(bill_date), MONTH(bill_date)
                HAVING COUNT(*) > 1
                """
            )).fetchall() if engine.dialect.name == "mysql" else []

            for row in rows:
                result["duplicates"].append(
                    f"user {row.user_id} / shop {row.shop_id} has {row.n} Rent bills "
                    f"for {row.y}-{row.m:02d} (bill ids {row.ids})"
                )

            duplicate_ids = set()
            for row in rows:
                duplicate_ids.update(int(i) for i in str(row.ids).split(","))

            if engine.dialect.name == "mysql":
                update = text(
                    """
                    UPDATE bills
                       SET rent_period = CONCAT('RENT-', DATE_FORMAT(bill_date, '%Y-%m'))
                     WHERE bill_type = 'Rent'
                       AND rent_period IS NULL
                    """
                    + (" AND id NOT IN :skip" if duplicate_ids else "")
                )
                if duplicate_ids:
                    update = update.bindparams(
                        bindparam("skip", expanding=True)
                    )
                    outcome = conn.execute(update, {"skip": sorted(duplicate_ids)})
                else:
                    outcome = conn.execute(update)
                conn.commit()
                result["filled"] = outcome.rowcount or 0
        except Exception as exc:
            conn.rollback()
            logger.warning("Could not backfill bills.rent_period: %s", exc)
            result["error"] = str(exc)

    if result["filled"]:
        print(f"  ✔  Backfilled rent_period on {result['filled']} existing Rent bill(s)")
    if result["duplicates"]:
        print(f"\n  ⚠  {len(result['duplicates'])} tenant/shop/month(s) already hold more than "
              f"one Rent bill. They are left unprotected by the new unique index\n"
              f"     until you decide which to keep:")
        for line in result["duplicates"]:
            print(f"       - {line}")
        print()
    return result


def split_penalties_into_bills() -> dict:
    """
    Move every late fee that currently lives in bills.penalty_amount into its
    own Penalty bill, linked to the bill it was charged for.

    Why this exists: a "Rent" bill for 10,000 that displayed 23,200 to pay was
    one row carrying two kinds of money. Tenants and admins could not tell
    which figure was rent and which was the fee, a payment could not be
    attributed to either, and rent income could not be reported apart from
    penalty income.

    HOW PAYMENTS ARE SPLIT - the part worth checking

    Existing payments were recorded against the combined figure, so where a
    tenant has paid more than the rent, some of that money belongs to the fee.
    It is allocated RENT FIRST: payments fill the rent, and only the excess
    moves to the fee bill. That is the conventional reading and the one that
    favours the tenant, since the rent is the older debt.

    A payment that straddles the boundary is SPLIT into two rows rather than
    moved whole - paid_amount has to equal the sum of a bill's own payments or
    the next reconciliation would silently undo this. Both halves keep the
    original date and method, and the moved half says in its remarks where it
    came from.

    Deliberately does NOT charge anyone anything new. Bills that were paid
    late but never had a fee applied are left alone: raising fees for settled
    history would bill people months after the fact for something they were
    never told about.

    Idempotent: a bill that already has a fee bill is skipped, so running the
    schema setup repeatedly is safe.
    """
    result = {"converted": 0, "payments_split": 0, "fees_total": 0.0, "warnings": []}
    db = SessionLocal()
    try:
        try:
            candidates = (
                db.query(Bill)
                .filter(Bill.penalty_amount > 0)
                .order_by(Bill.id)
                .all()
            )
        except Exception as exc:
            logger.warning("Could not read bills for the penalty split: %s", exc)
            return result

        if not candidates:
            return result

        already_split = {
            row.parent_bill_id
            for row in db.query(Bill.parent_bill_id)
            .filter(Bill.parent_bill_id.isnot(None)).all()
        }

        try:
            due_days = int(_setting_value(db, "bill.due_days", 30))
        except Exception:
            due_days = 30

        for parent in candidates:
            if parent.id in already_split:
                continue

            fee = Decimal(str(parent.penalty_amount or 0))
            rent = Decimal(str(parent.amount or 0))

            payments = sorted(parent.payments, key=lambda p: (p.payment_date, p.id))
            total_paid = sum(Decimal(str(p.amount or 0)) for p in payments)

            # ── Rent first; only the excess belongs to the fee ──
            fee_paid = max(Decimal("0"), total_paid - rent)
            if fee_paid > fee:
                result["warnings"].append(
                    f"bill #{parent.id}: payments of {total_paid} exceed rent {rent} "
                    f"plus fee {fee} by {fee_paid - fee} - the surplus was left on "
                    f"the late-fee bill, which now shows as overpaid"
                )

            when = parent.penalty_charged_through or (
                parent.bill_date.date() if parent.bill_date else now_utc().date()
            )
            fee_bill = Bill(
                user_id=parent.user_id,
                shop_id=parent.shop_id,
                bill_type="Penalty",
                description=f"Late fee on bill #{parent.id}",
                amount=fee,
                paid_amount=Decimal("0"),
                pending_amount=fee,
                bill_date=datetime.combine(when, datetime.min.time()),
                due_date=datetime.combine(when, datetime.min.time()) + timedelta(days=due_days),
                status="pending",
                penalty_amount=Decimal("0"),
                penalty_days=parent.penalty_days or 0,
                parent_bill_id=parent.id,
            )
            db.add(fee_bill)
            db.flush()

            # ── Move the excess payments across ──
            remaining_rent = rent
            moved = Decimal("0")
            for payment in payments:
                amount = Decimal(str(payment.amount or 0))
                if remaining_rent >= amount:
                    remaining_rent -= amount
                    continue

                keep = remaining_rent          # the part that still fits the rent
                move = amount - keep
                remaining_rent = Decimal("0")

                note = (f"Reallocated to late-fee bill #{fee_bill.id} when late fees "
                        f"were split out of bill #{parent.id}.")

                if keep > 0:
                    # Straddles the boundary: shrink this row, add the remainder
                    # to the fee bill as its own payment.
                    payment.amount = keep
                    db.add(Payment(
                        bill_id=fee_bill.id, amount=move,
                        payment_method=payment.payment_method,
                        payment_date=payment.payment_date,
                        remarks=(payment.remarks + " | " if payment.remarks else "") + note,
                    ))
                    result["payments_split"] += 1
                else:
                    # Entirely on the fee side - move the row itself.
                    payment.bill_id = fee_bill.id
                    payment.remarks = (payment.remarks + " | " if payment.remarks else "") + note
                moved += move

            # ── Restate both bills from their own payments ──
            fee_bill.paid_amount = moved
            fee_bill.pending_amount = max(Decimal("0"), fee - moved)
            fee_bill.status = ("paid" if moved >= fee else
                               "partial" if moved > 0 else "pending")

            parent.penalty_amount = Decimal("0")
            parent.penalty_days = 0
            parent.penalty_charged_through = None
            rent_paid = min(total_paid, rent)
            parent.paid_amount = rent_paid
            parent.pending_amount = max(Decimal("0"), rent - rent_paid)
            parent.status = ("paid" if rent_paid >= rent else
                             "partial" if rent_paid > 0 else "pending")

            result["converted"] += 1
            result["fees_total"] += float(fee)

        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Penalty split failed: %s", exc)
        result["warnings"].append(f"failed: {exc}")
    finally:
        db.close()

    if result["converted"]:
        print(f"  \u2714  Split {result['converted']} late fee(s) into their own bills "
              f"({result['fees_total']:.2f} total)")
        if result["payments_split"]:
            print(f"     {result['payments_split']} payment(s) split across rent and fee")
    for line in result["warnings"]:
        print(f"  \u26a0  {line}")
    return result


def _setting_value(db, key: str, fallback):
    """One app_settings value, without importing services/ (which imports this)."""
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is None or row.value is None:
        return fallback
    return row.value


def main():
    print("\n" + "═" * 50)
    print("  Tenant Management System – Database Setup")
    print("═" * 50 + "\n")

    # 1. Verify connectivity
    if not test_connection():
        print("❌  Cannot connect to the database. Check core/database.py / environment variables.")
        raise SystemExit(1)
    print("✔  Database Connected\n")

    # 2. Self-healing schema sync:
    #    - creates any tables missing entirely
    #    - adds any columns missing from existing tables
    #    - adds any indexes missing from existing tables
    print("Checking schema …")
    with engine.connect() as conn:
        summary = sync_schema(conn)

    if not any([summary["tables_created"], summary["columns_added"],
                summary["columns_relaxed"], summary["indexes_added"]]):
        print("✔  Schema already up to date – nothing to change.\n")
    else:
        print()
        if summary["tables_created"]:
            print(f"✔  Tables created: {len(summary['tables_created'])} -> {summary['tables_created']}")
        if summary["columns_added"]:
            print(f"✔  Columns added:  {len(summary['columns_added'])} -> {summary['columns_added']}")
        if summary["columns_relaxed"]:
            print(f"✔  Columns relaxed to NULL: {len(summary['columns_relaxed'])} -> {summary['columns_relaxed']}")
        if summary["indexes_added"]:
            print(f"✔  Indexes added:  {len(summary['indexes_added'])} -> {summary['indexes_added']}")
        print()
    if summary["errors"]:
        print(f"⚠  {len(summary['errors'])} schema change(s) could not be applied automatically:")
        for err in summary["errors"]:
            print(f"   - {err}")
        print()

    # 2b. Backfill bills.rent_period on existing Rent bills.
    backfill_rent_periods()

    # 2c. Move late fees out of bills.penalty_amount and into their own bills.
    split_penalties_into_bills()

    # 3. Seed default admin user
    db = SessionLocal()
    try:
        DEFAULT_MOBILE   = "8177809890"
        DEFAULT_PASSWORD = "Sujata8@Tekale8@"
        DEFAULT_NAME     = "Tejas Gund"

#        existing = db.query(User).filter(User.mobile == DEFAULT_MOBILE).first()
        existing = (
            db.query(User)
            .filter(
                or_(
                    User.email == "admin@tenantapp.com",
                    User.mobile == DEFAULT_MOBILE,
                )
            )
            .first()
        )
        if existing:
            print("ℹ  Default admin already exists – skipping seed.\n")
        else:
            admin = User(
                name          = DEFAULT_NAME,
                mobile        = DEFAULT_MOBILE,
                email         = "admin@tenantapp.com",
                password_hash = hash_password(DEFAULT_PASSWORD),
                role          = "admin",
                is_active     = True,
            )
            db.add(admin)
            db.commit()
            print("✔  Default Admin Created")
            print(f"   Mobile  : {DEFAULT_MOBILE}")
            print(f"   Password: {DEFAULT_PASSWORD}")
            print(f"   Role    : admin\n")

            # Audit the seed action
            log_entry = AuditLog(
                user_id    = admin.id,
                action     = "CREATE",
                table_name = "users",
                record_id  = admin.id,
                old_data   = None,
                new_data   = f'{{"mobile":"{DEFAULT_MOBILE}","role":"admin"}}',
            )
            db.add(log_entry)
            db.commit()

        # ── Seed a starting electricity tariff ──
        # Without at least one tariff row, approving a submeter reading has no
        # unit price to bill at. Seeded far in the past so it applies to any
        # reading date; the admin changes the rate from Settings -> Tariffs,
        # which adds a NEW row rather than editing this one (history is kept).
        existing_tariff = db.query(MeterTariff).filter(
            MeterTariff.meter_type == "electricity"
        ).first()
        if existing_tariff:
            print("ℹ  Electricity tariff already configured – skipping seed.\n")
        else:
            tariff = MeterTariff(
                meter_type     = "electricity",
                unit_price     = Decimal("8.00"),
                fixed_charge   = Decimal("0"),
                tax_percent    = Decimal("0"),
                effective_from = datetime(2000, 1, 1),
                notes          = "Default starting rate – update this from Settings.",
            )
            db.add(tariff)
            db.commit()
            print("✔  Default electricity tariff created: 8.00 per unit")
            print("   Change it in the admin UI (Settings → Tariffs) – a new rate is\n"
                  "   added with an effective date, so past bills keep their old price.\n")

    except Exception as exc:
        db.rollback()
        logger.error("Seed failed: %s", exc)
        print(f"❌  Seed error: {exc}")
        raise
    finally:
        db.close()

    print("═" * 50)
    print("  Setup complete. You can now start the API server.")
    print("  Run: uvicorn app:app --reload")
    print("═" * 50 + "\n")


if __name__ == "__main__":
    main()
