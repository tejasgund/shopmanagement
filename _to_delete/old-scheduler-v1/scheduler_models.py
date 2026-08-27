"""
scheduler/models.py - the scheduler's own ORM mapping.

This package no longer imports the application's models. It declares its own
SQLAlchemy Base and its own classes, which means the scheduler can be
installed, run and tested with the application's source nowhere on the box.

Two kinds of table are mapped here, and the difference matters:

  * OWNED - `scheduler_tasks`. The scheduler created it, is the only writer,
    and creates it on demand (see ensure_schema). Nothing else needs to exist
    for the scheduler to work.

  * MIRRORED - `users`, `shops`, `user_shops`, `bills`, `payments`,
    `app_settings`, `audit_logs`. These belong to the application. The
    scheduler maps only the columns it actually uses, and NEVER creates or
    alters them. The database itself is the contract between the two
    services, which is the normal arrangement once two processes share a
    database and the alternative - importing the other service's code - is
    exactly the coupling this package is meant not to have.

Mapping a subset is deliberate and safe for reading: SQLAlchemy selects the
columns it knows about, so a column added by the app is simply not fetched.
It is only INSERTs that care - a new NOT NULL column with no server-side
default would break bill creation here. verify_schema() below checks for
precisely that and says so in the log rather than failing at 2am with an
opaque driver error.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Enum, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, inspect as sa_inspect,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════════════════════════
# OWNED BY THE SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

class SchedulerTask(Base):
    """
    One row per expected run of one scheduled task - the scheduler's ledger.

    The database, not the crontab, is the source of truth for what was supposed
    to happen. A task is written here as PENDING the moment it is known to be
    due (including retrospectively, for a day the server was down), and every
    attempt updates the same row. That is what makes "nothing is silently
    missed" true: a run that never happened still exists as a PENDING row with
    a scheduled_for in the past, so it shows up as missed on the dashboard and
    gets picked up on the next sweep instead of vanishing.

    The unique constraint on (task_name, scheduled_for) is the duplicate
    protection. Registering the same occurrence twice - two cron ticks racing,
    a retry, a backfill overlapping a live run - is a no-op at the database
    level rather than something every caller has to remember to check.
    """

    __tablename__ = "scheduler_tasks"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    task_name     = Column(String(80), nullable=False, index=True)

    # When this occurrence was due to run.
    scheduled_for = Column(DateTime, nullable=False, index=True)
    # The business date it covers. Usually scheduled_for's date, but kept
    # separate because a backfilled run happening today is *for* an earlier day.
    run_date      = Column(Date, nullable=False, index=True)

    status = Column(
        Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED",
             name="scheduler_task_status"),
        nullable=False, default="PENDING", index=True,
    )

    attempts     = Column(Integer, nullable=False, default=0)
    started_at   = Column(DateTime, nullable=True)
    finished_at  = Column(DateTime, nullable=True)
    duration_ms  = Column(Integer, nullable=True)

    records_processed = Column(Integer, nullable=False, default=0)
    records_failed    = Column(Integer, nullable=False, default=0)

    error_message = Column(Text, nullable=True)
    # Whatever the task wants to show on the dashboard, as JSON.
    result_json   = Column(Text, nullable=True)
    # Why a run was skipped, so a SKIPPED row is never a mystery.
    skip_reason   = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=now_utc)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    __table_args__ = (
        UniqueConstraint("task_name", "scheduled_for", name="uq_scheduler_task_occurrence"),
        Index("ix_scheduler_tasks_status_scheduled", "status", "scheduled_for"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# MIRRORED FROM THE APPLICATION  (read, and write only to columns declared here)
# ══════════════════════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    name                   = Column(String(120), nullable=False)
    mobile                 = Column(String(15), nullable=False)
    # Day-of-month (1-28) this tenant's rent bill is raised on.
    rent_bill_date         = Column(Integer, nullable=True)
    # Per-tenant opt-in. A tenant with this off is never billed automatically,
    # however their rent_bill_date is set.
    auto_rent_bill_enabled = Column(Boolean, nullable=False, default=False)
    is_active              = Column(Boolean, nullable=False, default=True)

    user_shops = relationship("UserShop", back_populates="user")
    bills      = relationship("Bill", back_populates="user")


class Shop(Base):
    __tablename__ = "shops"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    shop_number = Column(String(50), nullable=False)
    # The single source of truth for what a rent bill is worth.
    shop_rent   = Column(Numeric(10, 2), nullable=False, default=0)

    user_shops = relationship("UserShop", back_populates="shop")
    bills      = relationship("Bill", back_populates="shop")


class UserShop(Base):
    __tablename__ = "user_shops"

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)

    user = relationship("User", back_populates="user_shops")
    shop = relationship("Shop", back_populates="user_shops")


class Bill(Base):
    __tablename__ = "bills"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    user_id        = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    shop_id        = Column(Integer, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False, index=True)
    bill_type      = Column(String(80), nullable=False)
    description    = Column(Text, nullable=True)
    amount         = Column(Numeric(12, 2), nullable=False)
    paid_amount    = Column(Numeric(12, 2), nullable=False, default=0)
    pending_amount = Column(Numeric(12, 2), nullable=False, default=0)
    bill_date      = Column(DateTime, nullable=False, default=now_utc)
    due_date       = Column(DateTime, nullable=True)
    status         = Column(Enum("pending", "partial", "paid", name="bill_status"),
                            nullable=False, default="pending")
    created_at     = Column(DateTime, nullable=False, default=now_utc)

    # ── Late-payment penalty ──────────────────────────────────────────────
    # `amount` is and stays the ORIGINAL bill: penalties never touch it, so
    # "what was this bill for" is always answerable however long it has been
    # overdue. What is owed = amount + penalty_amount, and that sum is what
    # pending_amount holds (see money.reconcile_bill, the one place here that
    # computes it).
    penalty_amount          = Column(Numeric(12, 2), nullable=False, default=0)
    # Chargeable days actually billed for - days past the due date MINUS the
    # grace period, not the raw overdue count.
    penalty_days            = Column(Integer, nullable=False, default=0)
    # The last date the penalty has been calculated up to. Informational: the
    # penalty is recomputed from scratch every run rather than incremented,
    # which is what makes a second run in the same day a no-op.
    penalty_charged_through = Column(Date, nullable=True)

    user     = relationship("User", back_populates="bills")
    shop     = relationship("Shop", back_populates="bills")
    payments = relationship("Payment", back_populates="bill")


class Payment(Base):
    __tablename__ = "payments"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    bill_id      = Column(Integer, ForeignKey("bills.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount       = Column(Numeric(12, 2), nullable=False)
    payment_date = Column(DateTime, nullable=False, default=now_utc)

    bill = relationship("Bill", back_populates="payments")


class AppSetting(Base):
    """
    Key/value configuration, shared with the application.

    The scheduler reads its own `scheduler.*` keys from here and writes none of
    them - the Scheduler settings screen in the app does the writing. Storing
    them in the database rather than in scheduler.conf is deliberate: a switch
    in a file on the server and a toggle in the UI would be two answers to the
    same question.
    """
    __tablename__ = "app_settings"

    id    = Column(Integer, primary_key=True, autoincrement=True)
    key   = Column(String(120), nullable=False, unique=True, index=True)
    value = Column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action     = Column(String(50), nullable=False)
    table_name = Column(String(80), nullable=True)
    record_id  = Column(Integer, nullable=True)
    old_data   = Column(Text, nullable=True)
    new_data   = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_utc)


# Tables the scheduler owns outright and may create. Everything else in this
# module maps a table the application owns and is never created from here.
OWNED_TABLES = (SchedulerTask.__table__,)

# Tables the scheduler writes rows INTO but does not own. These are the ones
# verify_schema checks, because an INSERT is what a column added upstream can
# break.
WRITTEN_TABLES = ("bills", "audit_logs")


def ensure_schema(engine) -> list:
    """
    Create the scheduler's own table if it is not there yet. Returns what it
    created, which is normally nothing.

    Only OWNED_TABLES: the scheduler must never invent the application's
    tables, because a scheduler pointed at the wrong database would otherwise
    quietly create an empty `bills` and start billing nobody.
    """
    created = []
    inspector = sa_inspect(engine)
    for table in OWNED_TABLES:
        if not inspector.has_table(table.name):
            table.create(bind=engine, checkfirst=True)
            created.append(table.name)
    return created


def verify_schema(engine) -> list:
    """
    Look for upstream schema changes that would break this process, and
    describe them. Returns a list of human-readable problems; empty is good.

    Checks, in the order they bite:
      * a table this process writes to is missing entirely
      * a column this process writes is missing from the live table
      * the live table has a NOT NULL column with no default that this process
        does not know about - the one shape of upstream change that turns an
        INSERT here into an integrity error

    Called at startup and logged, not raised: a warning at 02:00 that says
    "bills.late_fee_code is NOT NULL and I do not set it" is worth far more
    than a driver error nobody can read.
    """
    problems = []
    inspector = sa_inspect(engine)
    mapped = {t.name: t for t in Base.metadata.tables.values()}

    for table_name in WRITTEN_TABLES:
        table = mapped.get(table_name)
        if table is None:
            continue
        if not inspector.has_table(table_name):
            problems.append(f"table {table_name!r} does not exist in this database")
            continue

        live = {c["name"]: c for c in inspector.get_columns(table_name)}

        for column in table.columns:
            if column.name not in live:
                problems.append(
                    f"{table_name}.{column.name} is missing - this process expects it"
                )

        known = {c.name for c in table.columns}
        for name, spec in live.items():
            if name in known:
                continue
            if not spec.get("nullable", True) and spec.get("default") is None \
                    and not spec.get("autoincrement"):
                problems.append(
                    f"{table_name}.{name} is NOT NULL with no default and is unknown "
                    f"to the scheduler - inserts here will fail until it is mapped "
                    f"in scheduler/models.py"
                )

    return problems
