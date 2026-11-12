#!/usr/bin/env python3
"""
due_bill_penalty.py - applies the daily late fee to overdue unpaid bills.

Standalone. Reads its database details from db_config.py beside this file and
imports nothing else from this project - no application modules, no shared
helpers, no sibling scheduler. Run it from cron:

    5 2 * * * cd /opt/shopmanagement/scheduler/due_bill_penalty && \
              /usr/bin/python3 due_bill_penalty.py >> /dev/null 2>&1

    python3 due_bill_penalty.py                      # as of today
    python3 due_bill_penalty.py --date 2026-08-15    # as of that day
    python3 due_bill_penalty.py --dry-run            # calculate, change nothing

Requires: pymysql   (pip install pymysql)

──────────────────────────────────────────────────────────────────────────────
THE CALCULATION

    chargeable days = days between (due date + grace) and the as-of date
    penalty         = original bill amount x rate% x chargeable days
    capped at the configured maximum, if one is set

Worked example, the one to check any change against:

    10,000 due 10 Aug, 1%/day, no grace, run on 15 Aug
      -> 5 chargeable days x 100 = 500 penalty, 10,500 payable

`bills.amount` is NEVER touched. The penalty accumulates in penalty_amount, so
"what was this bill for" and "what is owed now" stay separately answerable
however long a bill has been outstanding. pending_amount carries the sum.

──────────────────────────────────────────────────────────────────────────────
WHY RUNNING IT TWICE IS SAFE

The penalty is RECOMPUTED FROM SCRATCH every time, never incremented. The
answer is a function of (original amount, due date, as-of date, settings) and
nothing else, so a second run in the same day, a retry after a failure, or a
backfill of a day the server was down all converge on the same number. An
incrementing implementation would need every one of those cases handled by
hand, and would double-charge the first time one was missed.

That is also why there is no duplicate-prevention machinery here, unlike the
rent scheduler: recomputation makes duplicates arithmetically impossible
rather than something to guard against.

──────────────────────────────────────────────────────────────────────────────
WHAT IT RECORDS

One scheduler_runs row per execution, and one scheduler_run_items row per bill
whose penalty CHANGED - carrying the rate, the grace, the day count and a
plain-language `reason` explaining the figure as it stood that night. That is
what lets someone answer "why does this bill say 11,400" months later, without
recomputing it from settings that may since have changed.

Bills examined and found already correct are counted but not written as rows:
on a database with thousands of overdue bills, a row per bill per night would
bury the changes that matter under unchanged ones.
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
        "due_bill_penalty: pymysql is not installed.\n"
        "    pip install pymysql\n"
    )
    raise SystemExit(1)

from db_config import DB_CONFIG


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

SCHEDULER_NAME = "due_bill_penalty"

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG_RETENTION_DAYS = 30

LOCK_NAME = "tms_due_bill_penalty"
LOCK_TIMEOUT_SECONDS = 30

# Read from app_settings so an admin can change the rate, grace and cap from
# the Settings screen without editing this file. The values here are only the
# fallback for a database where they have never been customised, and match the
# application's own defaults.
DEFAULTS = {
    "enabled": False,          # opt-in: turning it on starts charging tenants
    "percent_per_day": 1.0,
    "grace_days": 0,
    "max_amount": 0.0,         # 0 = no cap
    "on_penalty": False,       # charge a late fee on an unpaid late fee?
    "due_days": 30,            # how long a tenant gets to pay a late-fee bill
}
SETTING_KEYS = {
    "scheduler.penalty_enabled":            "enabled",
    "scheduler.penalty_percent_per_day":    "percent_per_day",
    "scheduler.penalty_grace_days":         "grace_days",
    "scheduler.penalty_max_amount":         "max_amount",
    "scheduler.penalty_on_penalty_enabled": "on_penalty",
    # Owned by the application's Settings screen, read here so a late-fee bill
    # gets the same payment window as every other bill.
    "bill.due_days":                        "due_days",
}

# Bill types that ARE late-fee bills. Whether they can themselves accrue a
# late fee is the "Charge a late fee on unpaid late fees" setting, off by
# default: compounding a fee into a fee grows a debt faster than a tenant can
# clear it, which is the behaviour a penalty is meant to discourage, not cause.
PENALTY_BILL_TYPES = {"penalty", "late fee", "latefee"}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DISABLED = 2
EXIT_BAD_USAGE = 3
EXIT_BUSY = 4


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING  (this scheduler's own files, nobody else's)
# ══════════════════════════════════════════════════════════════════════════════

def build_logger() -> logging.Logger:
    """
    Two files in logs/, rotated at midnight and kept for 30 days:

        logs/due_bill_penalty.log   everything this scheduler did
        logs/errors.log             ERROR and above only

    Entirely separate from the rent scheduler's logs. Neither writes to the
    other's directory, so "what did the penalty run do last night" is one file
    with nothing else interleaved into it.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(SCHEDULER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    for filename, level in (("due_bill_penalty.log", logging.INFO),
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

        PENALTY-20260827-020515-b7c2

    This is the string quoted when someone asks why a bill's balance went up.
    """
    return "PENALTY-{}-{}".format(now.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def connect():
    """One connection for the whole run, autocommit OFF - see run() on why
    each bill is its own transaction."""
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
    The penalty rules, read from app_settings.

    In the database rather than in this file because changing the rate is a
    business decision an admin makes on the Settings screen, not a code change
    someone SSHes in to make. An unreadable value falls back to the documented
    default and logs it, rather than failing the run.
    """
    settings = dict(DEFAULTS)
    try:
        placeholders = ", ".join(["%s"] * len(SETTING_KEYS))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT `key`, `value` FROM app_settings WHERE `key` IN ({})".format(placeholders),
                tuple(SETTING_KEYS),
            )
            for row in cur.fetchall():
                field = SETTING_KEYS[row["key"]]
                raw = row["value"]
                try:
                    if field in ("enabled", "on_penalty"):
                        settings[field] = str(raw).strip().lower() in ("1", "true", "yes", "on")
                    elif field in ("grace_days", "due_days"):
                        settings[field] = int(float(raw))
                    else:
                        settings[field] = float(raw)
                except (TypeError, ValueError):
                    log.warning("%s is unreadable (%r); using %s",
                                row["key"], raw, DEFAULTS[field])
    except Exception as exc:
        log.warning("Could not read app_settings (%s); using defaults.", exc)
    return settings


def acquire_lock(conn) -> bool:
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
    """Record the start before any work, so a killed run is still visible."""
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
                bill_id=None, amount=None, penalty_amount=None, penalty_days=None,
                penalty_rate=None, bill_due_date=None, reason=None,
                error_message=None) -> None:
    """One row per bill whose penalty changed - with the figures behind it."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scheduler_run_items
                (run_id, scheduler, run_date, user_id, user_name, shop_id,
                 shop_number, bill_id, action, status, amount, penalty_amount,
                 penalty_days, penalty_rate, bill_due_date, reason,
                 error_message, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, SCHEDULER_NAME, run_date, user_id, user_name, shop_id,
             shop_number, bill_id, action, status, amount, penalty_amount,
             penalty_days, penalty_rate, bill_due_date, reason, error_message,
             datetime.now()),
        )


def close_run(conn, run_id: str, started: datetime, status: str, summary: dict,
              error_message=None) -> None:
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
             summary["examined"], summary["changed"], summary["failed"],
             summary["unchanged"], summary["added"], error_message, run_id),
        )
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# BUSINESS LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def rent_covered_on(conn, bill_ids: list, as_of: date) -> dict:
    """
    For each bill, the date its OWN amount was fully covered by payments -
    or None if it still is not, as of `as_of`.

    This is the date the late fee stops. Without it a tenant who pays the rent
    but not yet the fee keeps accruing at 1% of the rent per day, so the fee
    grows faster than they can clear it and the debt outruns them. Paying the
    bill made the amount go up, which is exactly the behaviour tenants have
    been complaining about.

    Computed from the payment DATES rather than from "is it paid now", for two
    reasons that both matter:

      * The frozen figure must not depend on which night the script happened
        to run. Rent covered on the 15th freezes the fee at the 15th, whether
        this runs on the 16th or six weeks later.
      * Backfilling an earlier date must give the same answer it would have
        given then, so payments after `as_of` are ignored here. That is what
        keeps re-running and backfilling idempotent.
    """
    covered = {bill_id: None for bill_id in bill_ids}
    if not bill_ids:
        return covered

    placeholders = ", ".join(["%s"] * len(bill_ids))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.bill_id, p.payment_date, p.amount, b.amount AS bill_amount
              FROM payments p
              JOIN bills b ON b.id = p.bill_id
             WHERE p.bill_id IN ({})
               AND DATE(p.payment_date) <= %s
             ORDER BY p.bill_id, p.payment_date, p.id
            """.format(placeholders),
            tuple(bill_ids) + (as_of,),
        )
        rows = cur.fetchall()

    running = {}
    for row in rows:
        bill_id = row["bill_id"]
        if covered[bill_id] is not None:
            continue                      # already covered by an earlier payment
        running[bill_id] = running.get(bill_id, Decimal("0")) + Decimal(str(row["amount"] or 0))
        if running[bill_id] >= Decimal(str(row["bill_amount"] or 0)):
            paid_on = row["payment_date"]
            covered[bill_id] = paid_on.date() if isinstance(paid_on, datetime) else paid_on

    return covered


def overdue_bills(conn, as_of: date, include_penalty_bills: bool = False) -> list:
    """
    Every bill that could carry a penalty: not fully paid, has a due date,
    and is not itself a penalty.

    Late-fee bills are excluded unless `include_penalty_bills` says otherwise -
    see the setting above. Filtered here in Python rather than in SQL so the
    rule reads as one list of type names in one place, whichever way it is set.

    Deliberately NOT filtered by "past its grace period" here. A bill inside
    its grace period is still visited and simply quotes to zero, which keeps
    the grace rule in exactly one place - the calculation - rather than split
    between a query and a formula that must agree.

    The tenant and shop names come along for the ride so the tracking row can
    record who this was, without a second query per bill.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.id, b.user_id, b.shop_id, b.bill_type, b.amount,
                   b.due_date, b.status, b.penalty_amount, b.penalty_days,
                   u.name AS user_name, s.shop_number AS shop_number,
                   COALESCE((SELECT SUM(p.amount) FROM payments p
                              WHERE p.bill_id = b.id), 0) AS total_paid
              FROM bills b
         LEFT JOIN users u ON u.id = b.user_id
         LEFT JOIN shops s ON s.id = b.shop_id
             WHERE b.status <> 'paid'
               AND b.due_date IS NOT NULL
               AND DATE(b.due_date) <= %s
             ORDER BY b.id
            """,
            (as_of,),
        )
        rows = list(cur.fetchall())

    if include_penalty_bills:
        return rows
    return [r for r in rows
            if (r["bill_type"] or "").strip().lower() not in PENALTY_BILL_TYPES]


def quote(bill: dict, settings: dict, as_of: date, frozen_on: date = None) -> dict:
    """
    What the penalty on this bill is as of `as_of`, with every intermediate
    figure. Pure: reads the bill and the settings, changes nothing.

    Grace is measured from the due date, so a 5-day grace on a bill due on the
    10th starts charging on the 16th - the 15th is still free.

    `frozen_on` is the date the tenant covered the rent (see rent_covered_on).
    From that date the fee stops: it becomes a fixed amount they can actually
    clear, rather than one that grows every day they have not yet paid it.
    Charging a late fee on an unpaid late fee is a debt spiral, not a penalty.
    """
    if frozen_on is not None and frozen_on < as_of:
        as_of = frozen_on
    original = Decimal(str(bill["amount"] or 0))
    due = bill["due_date"]
    due_date = due.date() if isinstance(due, datetime) else due

    out = {
        "original": original,
        "due_date": due_date,
        # Set when the rent was covered and the fee therefore stopped. Carried
        # in the result so the caller records the date it was frozen at rather
        # than the night the script happened to run.
        "frozen_on": frozen_on,
        "days_overdue": 0,
        "grace_days": settings["grace_days"],
        "chargeable_days": 0,
        "rate": settings["percent_per_day"],
        "per_day": Decimal("0"),
        "penalty": Decimal("0"),
        "capped": False,
        "max_amount": settings["max_amount"],
    }
    if due_date is None or original <= 0:
        return out

    out["days_overdue"] = max(0, (as_of - due_date).days)
    chargeable = max(0, (as_of - (due_date + timedelta(days=settings["grace_days"]))).days)

    per_day = (original * Decimal(str(settings["percent_per_day"])) / Decimal("100")).quantize(Decimal("0.01"))
    penalty = (per_day * chargeable).quantize(Decimal("0.01"))

    if settings["max_amount"] > 0 and penalty > Decimal(str(settings["max_amount"])):
        penalty = Decimal(str(settings["max_amount"])).quantize(Decimal("0.01"))
        out["capped"] = True

    out["chargeable_days"] = chargeable
    out["per_day"] = per_day
    out["penalty"] = penalty
    return out


def explain(bill: dict, calc: dict, old_penalty: Decimal) -> str:
    """
    The sentence the dashboard shows: why this bill carries this penalty,
    in the figures that applied tonight.

    Written at decision time and stored, never recomputed. Settings change;
    what the tenant was actually charged does not.
    """
    if calc["chargeable_days"] <= 0:
        return ("Due {}. Still inside the {}-day grace period, so no penalty applies yet."
                .format(calc["due_date"], calc["grace_days"]))

    parts = [
        "Due {}, {} days overdue.".format(calc["due_date"], calc["days_overdue"]),
    ]
    if calc["grace_days"]:
        parts.append("{} grace days, leaving {} chargeable."
                     .format(calc["grace_days"], calc["chargeable_days"]))
    parts.append("{} chargeable days x {}% of the original {} ({}/day) = {}."
                 .format(calc["chargeable_days"], calc["rate"], calc["original"],
                         calc["per_day"], calc["penalty"]))
    if calc["capped"]:
        parts.append("Capped at the {} maximum per bill.".format(calc["max_amount"]))
    if calc.get("frozen_on"):
        parts.append("The rent on this bill was paid in full on {}, so the late fee "
                     "stopped there and will not grow further."
                     .format(calc["frozen_on"]))
    if old_penalty > calc["penalty"]:
        parts.append("Reduced from {} - the rules were relaxed or this was "
                     "recalculated for an earlier date.".format(old_penalty))
    parts.append("Charged as a separate Late fee bill; the rent bill stays "
                 "{}.".format(calc["original"]))
    return " ".join(parts)


def existing_fee_bills(conn, bill_ids: list) -> dict:
    """{parent_bill_id: fee bill row} for the bills being processed."""
    if not bill_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(bill_ids))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, parent_bill_id, amount, paid_amount, pending_amount, status
              FROM bills
             WHERE parent_bill_id IN ({})
            """.format(placeholders),
            tuple(bill_ids),
        )
        return {row["parent_bill_id"]: row for row in cur.fetchall()}


def upsert_fee_bill(conn, bill: dict, calc: dict, as_of: date, due_days: int,
                    existing: dict) -> tuple:
    """
    Make the late fee on `bill` equal `calc["penalty"]`, as a bill of its own.

    Returns (action, fee_bill_id, delta).

    ONE fee bill per parent, its amount recomputed while unpaid - not one bill
    per day, which for a bill 132 days overdue would mean 132 rows nobody could
    read. The unique index on parent_bill_id is what guarantees the "one".

    The fee is never reduced below what has already been PAID on it. Recompute
    is what makes re-running safe, but a relaxed setting must not turn money a
    tenant has handed over into a negative balance; the paid amount is the
    floor.
    """
    fee = calc["penalty"]
    row = existing.get(bill["id"])

    # ── Nothing owed, nothing recorded: leave the ledger alone ──
    if fee <= 0 and row is None:
        return ("NONE", None, Decimal("0"))

    # ── The fee has fallen to zero and nobody has paid any of it ──
    if fee <= 0 and row is not None:
        if Decimal(str(row["paid_amount"] or 0)) <= 0:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bills WHERE id = %s", (row["id"],))
            return ("PENALTY_BILL_CLEARED", row["id"], -Decimal(str(row["amount"] or 0)))
        # Partly paid: keep it, floored at what was paid, so the tenant is not
        # left with a bill for money they already handed over.
        fee = Decimal(str(row["paid_amount"]))

    paid = Decimal(str(row["paid_amount"] or 0)) if row else Decimal("0")
    if fee < paid:
        fee = paid

    raised_on = datetime.combine(as_of, datetime.min.time())
    status = "paid" if paid >= fee else ("partial" if paid > 0 else "pending")
    pending = max(Decimal("0"), fee - paid)

    if row is None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bills
                    (user_id, shop_id, bill_type, description, amount, paid_amount,
                     pending_amount, bill_date, due_date, status, penalty_amount,
                     penalty_days, parent_bill_id, created_at)
                VALUES (%s, %s, 'Penalty', %s, %s, 0, %s, %s, %s, 'pending', 0, %s, %s, %s)
                """,
                (bill["user_id"], bill["shop_id"],
                 "Late fee on bill #{}".format(bill["id"]),
                 fee, fee, raised_on, raised_on + timedelta(days=due_days),
                 calc["chargeable_days"], bill["id"], datetime.now()),
            )
            return ("PENALTY_BILL_RAISED", cur.lastrowid, fee)

    old = Decimal(str(row["amount"] or 0))
    if fee == old:
        return ("PENALTY_BILL_UNCHANGED", row["id"], Decimal("0"))

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bills
               SET amount = %s, pending_amount = %s, status = %s, penalty_days = %s
             WHERE id = %s
            """,
            (fee, pending, status, calc["chargeable_days"], row["id"]),
        )
    return ("PENALTY_BILL_UPDATED", row["id"], fee - old)


def mark_checked(conn, bill_id: int, as_of: date) -> None:
    """
    Record how far this bill's penalty has been calculated, even when the
    figure did not move.

    Without it, "checked and nothing was due" is indistinguishable from "never
    looked at", and only one of those is a problem.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE bills SET penalty_charged_through = %s WHERE id = %s",
                    (as_of, bill_id))


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(as_of: date, trigger_source: str, dry_run: bool) -> int:
    started = datetime.now()
    run_id = make_run_id(started)
    summary = {"examined": 0, "changed": 0, "unchanged": 0, "failed": 0,
               "added": Decimal("0")}

    log.info("=" * 70)
    log.info("Run %s | penalties as of %s%s",
             run_id, as_of, " | DRY RUN - nothing will be written" if dry_run else "")

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
            log.info("Run %s | skipped: the due-date penalty is OFF in Settings", run_id)
            return EXIT_DISABLED

        if not acquire_lock(conn):
            log.warning("Run %s | another penalty run holds the lock; exiting without "
                        "doing anything", run_id)
            return EXIT_BUSY

        try:
            log.info("Run %s | %s%%/day, %s grace day(s), cap %s, late fee on late fees: %s",
                     run_id, settings["percent_per_day"], settings["grace_days"],
                     settings["max_amount"] or "none",
                     "ON" if settings["on_penalty"] else "off")

            if not dry_run:
                open_run(conn, run_id, as_of, started, trigger_source)

            candidates = overdue_bills(conn, as_of, settings["on_penalty"])
            bill_ids = [b["id"] for b in candidates]
            # Two queries for the whole run rather than two per bill: this runs
            # over every overdue bill in the database, every night.
            frozen = rent_covered_on(conn, bill_ids, as_of)
            fee_bills = existing_fee_bills(conn, bill_ids)
            due_days = settings["due_days"]

            for bill in candidates:
                summary["examined"] += 1
                label = "bill #{} ({} / shop {})".format(
                    bill["id"], bill["user_name"], bill["shop_number"])

                try:
                    calc = quote(bill, settings, as_of, frozen.get(bill["id"]))
                    existing = fee_bills.get(bill["id"])
                    old_fee = Decimal(str(existing["amount"] or 0)) if existing else Decimal("0")

                    if dry_run:
                        # Decide and report; touch nothing.
                        if calc["penalty"] == old_fee:
                            summary["unchanged"] += 1
                        else:
                            summary["changed"] += 1
                            log.info("  WOULD %s | fee %s -> %s (%s days)",
                                     label, old_fee, calc["penalty"], calc["chargeable_days"])
                        conn.rollback()
                        continue

                    action, fee_bill_id, delta = upsert_fee_bill(
                        conn, bill, calc, calc.get("frozen_on") or as_of,
                        due_days, fee_bills,
                    )

                    if action in ("NONE", "PENALTY_BILL_UNCHANGED"):
                        summary["unchanged"] += 1
                        mark_checked(conn, bill["id"], calc.get("frozen_on") or as_of)
                        conn.commit()
                        continue

                    record_item(
                        conn, run_id, as_of, action=action, status="SUCCESS",
                        user_id=bill["user_id"], user_name=bill["user_name"],
                        shop_id=bill["shop_id"], shop_number=bill["shop_number"],
                        # The fee bill is what changed, so that is what the
                        # tracking row points at; the parent is named in the
                        # reason so both ends are findable.
                        bill_id=fee_bill_id, amount=delta,
                        penalty_amount=calc["penalty"],
                        penalty_days=calc["chargeable_days"],
                        penalty_rate=calc["rate"],
                        bill_due_date=bill["due_date"],
                        reason=explain(bill, calc, old_fee),
                    )
                    mark_checked(conn, bill["id"], calc.get("frozen_on") or as_of)
                    conn.commit()

                    summary["changed"] += 1
                    if delta > 0:
                        summary["added"] += delta
                    log.info("  %-22s %s | fee %s -> %s (%s days) as bill #%s",
                             action, label, old_fee, calc["penalty"],
                             calc["chargeable_days"], fee_bill_id)

                except Exception as exc:
                    conn.rollback()
                    summary["failed"] += 1
                    log.error("  FAIL  %s - %s", label, exc, exc_info=True)
                    if not dry_run:
                        record_item(
                            conn, run_id, as_of, action="FAILED", status="FAILED",
                            user_id=bill["user_id"], user_name=bill["user_name"],
                            shop_id=bill["shop_id"], shop_number=bill["shop_number"],
                            bill_id=bill["id"], bill_due_date=bill["due_date"],
                            reason="The penalty for this bill could not be calculated "
                                   "or written.",
                            error_message=str(exc)[:2000],
                        )
                        conn.commit()

            status = "FAILED" if (summary["failed"] and not summary["changed"]) else (
                "PARTIAL" if summary["failed"] else "SUCCESS"
            )
            if not dry_run:
                close_run(conn, run_id, started, status, summary)

            log.info("Run %s | %s | %s examined, %s changed (+%s), %s unchanged, %s failed",
                     run_id, status, summary["examined"], summary["changed"],
                     summary["added"], summary["unchanged"], summary["failed"])
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
        prog="due_bill_penalty.py",
        description="Apply the daily late fee to overdue bills. Intended to be run by cron.",
    )
    parser.add_argument("--date", help="YYYY-MM-DD to calculate as of; defaults to today")
    parser.add_argument("--dry-run", action="store_true",
                        help="calculate everything, write nothing")
    parser.add_argument("--manual", action="store_true",
                        help="record this run as manually triggered rather than cron")
    args = parser.parse_args(argv)

    if args.date:
        try:
            as_of = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("--date must be YYYY-MM-DD, got %r", args.date)
            return EXIT_BAD_USAGE
    else:
        as_of = date.today()

    return run(as_of, "manual" if args.manual else "cron", args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
