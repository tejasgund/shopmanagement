#!/usr/bin/env python3
"""
auto_rent_generation.py - creates each tenant's monthly Rent bill.

Standalone. Reads its database details from db_config.py beside this file and
imports nothing else from this project - no application modules, no shared
helpers, no sibling scheduler. Run it from cron:

    0 2 * * * cd /opt/shopmanagement/scheduler/auto_rent_generation && \
              /usr/bin/python3 auto_rent_generation.py >> /dev/null 2>&1

    python3 auto_rent_generation.py                 # today
    python3 auto_rent_generation.py --date 2026-09-05   # as if run that day
    python3 auto_rent_generation.py --dry-run       # decide, change nothing

Requires: pymysql   (pip install pymysql)

──────────────────────────────────────────────────────────────────────────────
WHO GETS BILLED

A tenant is billed for a month when all of these hold:

    active  AND  auto_rent_bill_enabled  AND  rent_bill_date has ARRIVED
    AND no Rent bill already exists for that tenant/shop/month

"has arrived" rather than "is exactly today" is deliberate. If the server is
down on the 5th, a tenant whose rent day is the 5th would - under an
exactly-today rule - never be billed for that month at all, and nothing would
ever notice. Here the next nightly run picks them up, and the tracking row
records both dates, so the report reads "due on the 5th, created on the 7th"
instead of silently skipping a month's rent.

──────────────────────────────────────────────────────────────────────────────
WHY IT CANNOT DOUBLE-BILL

Three independent layers, because a duplicate rent bill is the worst thing
this script can do:

  1. bills.rent_period + the UNIQUE index (user_id, shop_id, rent_period).
     A second Rent bill for the same tenant/shop/month is refused BY THE
     DATABASE. Not a convention - a constraint.
  2. A MySQL named lock (GET_LOCK) held for the whole run, so two cron entries,
     or a manual run overlapping the nightly one, cannot interleave.
  3. The existence check below, which is what turns "refused" into a tidy
     SKIPPED_DUPLICATE tracking row instead of an error.

Layer 1 is the one that actually guarantees it. Layers 2 and 3 exist so the
guarantee is reached quietly rather than as a crash.

──────────────────────────────────────────────────────────────────────────────
WHAT IT RECORDS

Every run writes one scheduler_runs row and one scheduler_run_items row per
tenant/shop it considered - including the ones it decided NOT to bill. A
skipped duplicate that leaves no trace is indistinguishable from a tenant the
script never looked at, and those need to be told apart when someone asks why
a bill is missing.
"""

import argparse
import logging
import os
import socket
import sys
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:                                   # pragma: no cover
    sys.stderr.write(
        "auto_rent_generation: pymysql is not installed.\n"
        "    pip install pymysql\n"
    )
    raise SystemExit(1)

from db_config import DB_CONFIG


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

SCHEDULER_NAME = "auto_rent_generation"

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG_RETENTION_DAYS = 30

# Held for the whole run. Any other process taking the same name waits, then
# gives up and exits cleanly rather than running alongside.
LOCK_NAME = "tms_auto_rent_generation"
LOCK_TIMEOUT_SECONDS = 30

# Read from app_settings if present, so an admin can change the due period from
# the Settings screen without editing this file. The value here is only the
# fallback for a database where the setting has never been customised, and
# matches the application's own default.
DEFAULT_BILL_DUE_DAYS = 30
SETTING_DUE_DAYS = "bill.due_days"
SETTING_ENABLED = "scheduler.rent_generation_enabled"

# Exit codes, so cron mail and monitoring can tell outcomes apart.
EXIT_OK = 0             # ran (including "ran and had nothing to do")
EXIT_FAILED = 1         # the run itself failed - see the log
EXIT_DISABLED = 2       # switched off in Settings; nothing attempted
EXIT_BAD_USAGE = 3      # unparseable arguments
EXIT_BUSY = 4           # another run holds the lock; its run covers this work


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING  (this scheduler's own files, nobody else's)
# ══════════════════════════════════════════════════════════════════════════════

def build_logger() -> logging.Logger:
    """
    Two files in logs/, rotated at midnight and kept for 30 days:

        logs/auto_rent_generation.log   everything this scheduler did
        logs/errors.log                 ERROR and above only

    The second is not a duplicate for its own sake: the first answers "what
    did the run do", the second answers "did anything break last night", and
    that is the question monitoring actually asks. Console output as well, so
    cron mails a summary when something goes wrong.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(SCHEDULER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    for filename, level in (("auto_rent_generation.log", logging.INFO),
                            ("errors.log", logging.ERROR)):
        handler = TimedRotatingFileHandler(
            os.path.join(LOG_DIR, filename), when="midnight", interval=1,
            backupCount=LOG_RETENTION_DAYS, encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(fmt)
        logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


log = build_logger()


# ══════════════════════════════════════════════════════════════════════════════
# RUN ID
# ══════════════════════════════════════════════════════════════════════════════

def make_run_id(now: datetime) -> str:
    """
    A readable, sortable handle for one execution:

        AUTO_RENT-20260827-020015-3f9a

    Readable rather than a bare UUID because this is the string someone quotes
    when asking why a bill looks the way it does. The random suffix keeps two
    runs started in the same second apart.
    """
    return "AUTO_RENT-{}-{}".format(
        now.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4]
    )


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def connect():
    """
    One connection for the whole run, with autocommit OFF.

    Every write below is inside an explicit transaction so a failure part-way
    through a tenant leaves no half-created bill.
    """
    return pymysql.connect(
        host=DB_CONFIG["host"],
        port=int(DB_CONFIG["port"]),
        database=DB_CONFIG["database"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )


def read_settings(conn) -> dict:
    """
    The two settings this scheduler obeys, read from app_settings.

    Kept in the database rather than in this file so the due period and the
    on/off switch can be changed from the admin Settings screen without
    editing Python on a production server. A missing table or a malformed
    value falls back to the defaults above and says so - a scheduler that
    refuses to run because one setting is unreadable is worse than one that
    uses the documented default and tells you.
    """
    settings = {"due_days": DEFAULT_BILL_DUE_DAYS, "enabled": True}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT `key`, `value` FROM app_settings WHERE `key` IN (%s, %s)",
                (SETTING_DUE_DAYS, SETTING_ENABLED),
            )
            for row in cur.fetchall():
                if row["key"] == SETTING_DUE_DAYS:
                    try:
                        settings["due_days"] = int(float(row["value"]))
                    except (TypeError, ValueError):
                        log.warning("%s is unreadable (%r); using %s",
                                    SETTING_DUE_DAYS, row["value"], DEFAULT_BILL_DUE_DAYS)
                elif row["key"] == SETTING_ENABLED:
                    settings["enabled"] = str(row["value"]).strip().lower() in (
                        "1", "true", "yes", "on",
                    )
    except Exception as exc:
        log.warning("Could not read app_settings (%s); using defaults.", exc)
    return settings


def acquire_lock(conn) -> bool:
    """Take the run lock. False means another run already holds it."""
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s) AS ok", (LOCK_NAME, LOCK_TIMEOUT_SECONDS))
        return bool((cur.fetchone() or {}).get("ok"))


def release_lock(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
    except Exception:
        log.warning("Could not release the run lock", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def open_run(conn, run_id: str, run_date: date, started: datetime,
             trigger_source: str) -> None:
    """Record that a run has started, before any work is attempted.

    Written and committed up front on purpose: a run that is killed mid-way
    still leaves a RUNNING row with a start time, which is how a crashed run
    is distinguishable from one that never started.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scheduler_runs
                (run_id, scheduler, run_date, started_at, status,
                 trigger_source, items_total, items_succeeded, items_failed,
                 items_skipped, amount_total, hostname, created_at)
            VALUES (%s, %s, %s, %s, 'RUNNING', %s, 0, 0, 0, 0, 0, %s, %s)
            """,
            (run_id, SCHEDULER_NAME, run_date, started, trigger_source,
             socket.gethostname()[:120], started),
        )
    conn.commit()


def record_item(conn, run_id: str, run_date: date, *, action: str, status: str,
                user_id=None, user_name=None, shop_id=None, shop_number=None,
                bill_id=None, amount=None, period_key=None, reason=None,
                error_message=None) -> None:
    """One row per tenant/shop considered - billed, skipped or failed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scheduler_run_items
                (run_id, scheduler, run_date, user_id, user_name, shop_id,
                 shop_number, bill_id, action, status, amount, period_key,
                 reason, error_message, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, SCHEDULER_NAME, run_date, user_id, user_name, shop_id,
             shop_number, bill_id, action, status, amount, period_key,
             reason, error_message, datetime.now()),
        )


def close_run(conn, run_id: str, started: datetime, status: str, summary: dict,
              error_message=None) -> None:
    """Finalise the run row with its outcome and totals."""
    finished = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scheduler_runs
               SET finished_at = %s, duration_ms = %s, status = %s,
                   items_total = %s, items_succeeded = %s, items_failed = %s,
                   items_skipped = %s, amount_total = %s, error_message = %s
             WHERE run_id = %s
            """,
            (finished, int((finished - started).total_seconds() * 1000), status,
             summary["total"], summary["created"], summary["failed"],
             summary["skipped"], summary["amount"], error_message, run_id),
        )
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# BUSINESS LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def period_key_for(run_date: date) -> str:
    """'RENT-2026-09' - the month a rent bill belongs to."""
    return "RENT-{:04d}-{:02d}".format(run_date.year, run_date.month)


def due_tenants(conn, run_date: date) -> list:
    """
    Every tenant/shop pair whose rent is due this month and not yet billed.

    One query rather than a loop of queries: the whole decision - opted in,
    active, day arrived, shop assigned, not already billed - is expressed here
    so there is exactly one place to read to know who gets billed and why.

    The LEFT JOIN on an existing Rent bill is what makes a re-run harmless: a
    tenant already billed for this month simply does not come back.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id           AS user_id,
                   u.name         AS user_name,
                   u.rent_bill_date AS rent_day,
                   s.id           AS shop_id,
                   s.shop_number  AS shop_number,
                   s.shop_rent    AS shop_rent,
                   b.id           AS existing_bill_id
              FROM users u
              JOIN user_shops us ON us.user_id = u.id
              JOIN shops s       ON s.id = us.shop_id
              LEFT JOIN bills b  ON b.user_id = u.id
                                AND b.shop_id = s.id
                                AND b.bill_type = 'Rent'
                                AND YEAR(b.bill_date)  = %s
                                AND MONTH(b.bill_date) = %s
             WHERE u.is_active = 1
               AND u.auto_rent_bill_enabled = 1
               AND u.rent_bill_date IS NOT NULL
               AND u.rent_bill_date <= %s
             ORDER BY u.id, s.id
            """,
            (run_date.year, run_date.month, run_date.day),
        )
        return list(cur.fetchall())


def tenants_without_shops(conn, run_date: date) -> list:
    """
    Opted-in tenants whose rent day has arrived but who have no shop assigned.

    Reported rather than ignored: "nothing to bill" and "nobody looked" are
    different answers, and only one of them needs someone to act.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id AS user_id, u.name AS user_name,
                   u.rent_bill_date AS rent_day
              FROM users u
         LEFT JOIN user_shops us ON us.user_id = u.id
             WHERE u.is_active = 1
               AND u.auto_rent_bill_enabled = 1
               AND u.rent_bill_date IS NOT NULL
               AND u.rent_bill_date <= %s
               AND us.id IS NULL
             ORDER BY u.id
            """,
            (run_date.day,),
        )
        return list(cur.fetchall())


def create_rent_bill(conn, row: dict, run_date: date, due_days: int,
                     period_key: str) -> tuple:
    """
    Insert one Rent bill. Returns (bill_id, amount, due_date, bill_date).

    bill_date is the tenant's OWN rent day in this month, not the day the
    script happened to run. A catch-up run on the 7th for a rent day of the
    5th produces a bill dated the 5th, so the due date - and therefore any
    later penalty - is the one the tenant was always going to get. The run
    being late must not cost them days.
    """
    rent_day = int(row["rent_day"])
    bill_date = datetime(run_date.year, run_date.month, rent_day)
    due_date = bill_date + timedelta(days=due_days)
    amount = Decimal(str(row["shop_rent"] or 0))

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bills
                (user_id, shop_id, bill_type, description, amount, paid_amount,
                 pending_amount, bill_date, due_date, status, penalty_amount,
                 penalty_days, rent_period, created_at)
            VALUES (%s, %s, 'Rent', %s, %s, 0, %s, %s, %s, 'pending', 0, 0, %s, %s)
            """,
            (row["user_id"], row["shop_id"], "Auto-generated monthly rent",
             amount, amount, bill_date, due_date, period_key, datetime.now()),
        )
        bill_id = cur.lastrowid

        # The same audit row an admin's manual bill creation writes, with
        # user_id NULL: no human took this action, and the trail should say so.
        cur.execute(
            """
            INSERT INTO audit_logs
                (user_id, action, table_name, record_id, new_data, created_at)
            VALUES (NULL, 'AUTO_GENERATE', 'bills', %s, %s, %s)
            """,
            (bill_id,
             '{{"user_id": {}, "shop_id": {}, "amount": {}, "bill_date": "{}"}}'.format(
                 row["user_id"], row["shop_id"], amount, bill_date.isoformat()),
             datetime.now()),
        )

    return bill_id, amount, due_date, bill_date


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(run_date: date, trigger_source: str, dry_run: bool) -> int:
    started = datetime.now()
    run_id = make_run_id(started)
    summary = {"total": 0, "created": 0, "skipped": 0, "failed": 0,
               "amount": Decimal("0")}

    log.info("=" * 70)
    log.info("Run %s | rent generation for %s%s",
             run_id, run_date, " | DRY RUN - nothing will be written" if dry_run else "")

    try:
        conn = connect()
    except Exception as exc:
        log.error("Cannot connect to %s@%s:%s/%s - %s",
                  DB_CONFIG["user"], DB_CONFIG["host"], DB_CONFIG["port"],
                  DB_CONFIG["database"], exc)
        return EXIT_FAILED

    try:
        settings = read_settings(conn)
        if not settings["enabled"]:
            log.info("Run %s | skipped: automatic rent generation is OFF in Settings", run_id)
            return EXIT_DISABLED

        if not acquire_lock(conn):
            log.warning("Run %s | another rent run holds the lock; that run covers "
                        "this work, so this one exits without doing anything", run_id)
            return EXIT_BUSY

        try:
            period_key = period_key_for(run_date)
            due_days = settings["due_days"]
            log.info("Run %s | period %s | due period %s days",
                     run_id, period_key, due_days)

            if not dry_run:
                open_run(conn, run_id, run_date, started, trigger_source)

            # ── Tenants with no shop: nothing to bill, but worth recording ──
            for row in tenants_without_shops(conn, run_date):
                summary["total"] += 1
                summary["skipped"] += 1
                reason = ("Rent day {} has arrived but this tenant has no shop "
                          "assigned, so there is nothing to bill."
                          .format(row["rent_day"]))
                log.info("  SKIP  %s (no shop assigned)", row["user_name"])
                if not dry_run:
                    record_item(conn, run_id, run_date, action="SKIPPED_NO_SHOP",
                                status="SKIPPED", user_id=row["user_id"],
                                user_name=row["user_name"], period_key=period_key,
                                reason=reason)

            # ── The real work ──
            for row in due_tenants(conn, run_date):
                summary["total"] += 1
                label = "{} / shop {}".format(row["user_name"], row["shop_number"])

                # Already billed this month - the ordinary outcome of a re-run,
                # a catch-up, or a manually created rent bill.
                if row["existing_bill_id"]:
                    summary["skipped"] += 1
                    reason = ("A Rent bill for {} already exists for this tenant and "
                              "shop (bill #{}), so no second one was created."
                              .format(period_key.replace("RENT-", ""), row["existing_bill_id"]))
                    log.info("  SKIP  %s (already billed, bill #%s)",
                             label, row["existing_bill_id"])
                    if not dry_run:
                        record_item(conn, run_id, run_date, action="SKIPPED_DUPLICATE",
                                    status="SKIPPED", user_id=row["user_id"],
                                    user_name=row["user_name"], shop_id=row["shop_id"],
                                    shop_number=row["shop_number"],
                                    bill_id=row["existing_bill_id"],
                                    period_key=period_key, reason=reason)
                    continue

                # A shop with no rent set would produce a zero-value bill.
                rent = Decimal(str(row["shop_rent"] or 0))
                if rent <= 0:
                    summary["skipped"] += 1
                    reason = ("Shop {} has no rent amount set, so no bill was raised. "
                              "Set the shop's rent to bill this tenant."
                              .format(row["shop_number"]))
                    log.info("  SKIP  %s (shop rent is zero)", label)
                    if not dry_run:
                        record_item(conn, run_id, run_date, action="SKIPPED_ZERO_RENT",
                                    status="SKIPPED", user_id=row["user_id"],
                                    user_name=row["user_name"], shop_id=row["shop_id"],
                                    shop_number=row["shop_number"],
                                    period_key=period_key, reason=reason)
                    continue

                # One transaction per tenant/shop: a failure here rolls back
                # this bill only, and every other tenant still gets theirs.
                try:
                    bill_id, amount, due_date, bill_date = create_rent_bill(
                        conn, row, run_date, due_days, period_key,
                    )
                    reason = ("Rent day {} for {}. {} monthly rent for shop {}, "
                              "dated {} and due {} ({} day period)."
                              .format(row["rent_day"], period_key.replace("RENT-", ""),
                                      amount, row["shop_number"],
                                      bill_date.date(), due_date.date(), due_days))
                    if run_date.day != int(row["rent_day"]):
                        reason += (" Created on {} - later than the rent day, so this "
                                   "was a catch-up for a run that did not happen on "
                                   "the day itself.".format(run_date))
                    if not dry_run:
                        record_item(conn, run_id, run_date, action="RENT_CREATED",
                                    status="SUCCESS", user_id=row["user_id"],
                                    user_name=row["user_name"], shop_id=row["shop_id"],
                                    shop_number=row["shop_number"], bill_id=bill_id,
                                    amount=amount, period_key=period_key, reason=reason)
                        conn.commit()
                    else:
                        conn.rollback()
                    summary["created"] += 1
                    summary["amount"] += amount
                    log.info("  BILL  %s -> %s (bill #%s, due %s)",
                             label, amount, bill_id, due_date.date())

                except pymysql.err.IntegrityError as exc:
                    # The unique index refused it. Another process created the
                    # same bill between our check and our insert - exactly what
                    # the index is for. Not an error: the bill exists.
                    conn.rollback()
                    summary["skipped"] += 1
                    reason = ("A concurrent run created this tenant's {} rent bill "
                              "first; the database refused the duplicate, which is "
                              "the protection working as intended."
                              .format(period_key.replace("RENT-", "")))
                    log.info("  SKIP  %s (duplicate refused by the database)", label)
                    if not dry_run:
                        record_item(conn, run_id, run_date, action="SKIPPED_DUPLICATE",
                                    status="SKIPPED", user_id=row["user_id"],
                                    user_name=row["user_name"], shop_id=row["shop_id"],
                                    shop_number=row["shop_number"],
                                    period_key=period_key, reason=reason,
                                    error_message=str(exc)[:500])
                        conn.commit()

                except Exception as exc:
                    conn.rollback()
                    summary["failed"] += 1
                    log.error("  FAIL  %s - %s", label, exc, exc_info=True)
                    if not dry_run:
                        record_item(conn, run_id, run_date, action="FAILED",
                                    status="FAILED", user_id=row["user_id"],
                                    user_name=row["user_name"], shop_id=row["shop_id"],
                                    shop_number=row["shop_number"],
                                    period_key=period_key,
                                    reason="Rent bill could not be created.",
                                    error_message=str(exc)[:2000])
                        conn.commit()

            # PARTIAL, not FAILED: the run did useful work for everyone else,
            # and re-running it blindly would not fix the rows that failed.
            status = "FAILED" if (summary["failed"] and not summary["created"]) else (
                "PARTIAL" if summary["failed"] else "SUCCESS"
            )
            if not dry_run:
                close_run(conn, run_id, started, status, summary)

            log.info("Run %s | %s | %s considered, %s billed (%s), %s skipped, %s failed",
                     run_id, status, summary["total"], summary["created"],
                     summary["amount"], summary["skipped"], summary["failed"])
            return EXIT_OK

        finally:
            release_lock(conn)

    except Exception as exc:
        log.error("Run %s failed: %s", run_id, exc, exc_info=True)
        try:
            conn.rollback()
            if not dry_run:
                close_run(conn, run_id, started, "FAILED", summary, str(exc)[:2000])
        except Exception:
            log.error("Could not record the failure against run %s", run_id, exc_info=True)
        return EXIT_FAILED
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="auto_rent_generation.py",
        description="Create each tenant's monthly Rent bill. Intended to be run by cron.",
    )
    parser.add_argument("--date", help="YYYY-MM-DD to run for; defaults to today")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide everything, write nothing")
    parser.add_argument("--manual", action="store_true",
                        help="record this run as manually triggered rather than cron")
    args = parser.parse_args(argv)

    if args.date:
        try:
            run_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("--date must be YYYY-MM-DD, got %r", args.date)
            return EXIT_BAD_USAGE
    else:
        run_date = date.today()

    return run(run_date, "manual" if args.manual else "cron", args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
