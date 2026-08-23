"""
rent_billing.py - automatic monthly rent bill generation.

Extracted from app.py when the scheduler moved out of the application and
under crontab (see scheduler/). It lives here, outside both app.py and
scheduler/, because there are now TWO callers that must run byte-identical
logic: the admin's manual POST /api/bills/generate-rent, and the cron
runner. Duplicating it into the scheduler folder would be the surest way to
have the nightly run and the manual button quietly disagree about what a
rent bill is.

Nothing here imports FastAPI or the app module, so the cron process can
import it without constructing the whole web application.

Deliberately session-driven: every entry point takes the `db` session to
work in, and the generation lock derives its own connection from that
session's engine rather than from a module-level one, so the API process and
a standalone cron process can each supply their own database setup.
"""

import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import extract
from sqlalchemy.orm import Session

from create_tables import Bill, Shop, User, UserShop
from audit_service import write_audit
from domain_helpers import _decimal_to_float
from log import get_logger
import settings_service

logger = get_logger("app")


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
            # The caller's engine, not a module-level one - see the module
            # docstring. Still a SEPARATE connection from `db` for the reason
            # spelled out above.
            conn = db.get_bind().connect()
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
