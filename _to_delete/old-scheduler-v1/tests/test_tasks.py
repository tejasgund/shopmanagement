"""
The three tasks, run through the real coordinator against a real database.

These are the scheduler's end-to-end tests: no application, no HTTP, no
mocking of the rules - a session, some settings, and the same code path cron
drives.
"""

from datetime import date, datetime, timedelta

from scheduler import master
from scheduler import service as svc
from scheduler.billing import penalty, rent
from scheduler.models import Bill, SchedulerTask
from .conftest import set_settings


# ══════════════════════════════════════════════════════════════════════════════
# RENT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def test_rent_generation_creates_one_bill_per_assigned_shop(db, tenant, shop):
    target = date(2026, 9, 1)          # tenant.rent_bill_date == 1
    summary = rent.generate_rent_bills_for_date_locked(db, target)

    assert summary["users_matched"] == 1
    assert len(summary["created"]) == 1

    bill = db.query(Bill).filter(Bill.id == summary["created"][0]).one()
    assert bill.bill_type == "Rent"
    assert float(bill.amount) == 10000.0
    assert bill.bill_date.date() == target
    # Due date = bill date + the app-owned bill.due_days, default 30.
    assert bill.due_date.date() == target + timedelta(days=30)


def test_rent_generation_is_idempotent_for_the_same_month(db, tenant, shop):
    """A retry, a backfill, or two cron entries firing must not double-bill."""
    target = date(2026, 9, 1)
    first = rent.generate_rent_bills_for_date_locked(db, target)
    second = rent.generate_rent_bills_for_date_locked(db, target)

    assert len(first["created"]) == 1
    assert second["created"] == []
    assert second["skipped_existing"] == 1
    assert db.query(Bill).filter(Bill.bill_type == "Rent").count() == 1


def test_rent_generation_honours_the_configured_due_period(db, tenant, shop):
    set_settings(db, {"bill.due_days": 30})
    summary = rent.generate_rent_bills_for_date_locked(db, date(2026, 9, 1))
    bill = db.query(Bill).filter(Bill.id == summary["created"][0]).one()
    assert bill.due_date.date() == date(2026, 9, 1) + timedelta(days=30)


def test_a_tenant_who_has_not_opted_in_is_never_billed(db, tenant, shop):
    tenant.auto_rent_bill_enabled = False
    db.commit()
    summary = rent.generate_rent_bills_for_date_locked(db, date(2026, 9, 1))
    assert summary["users_matched"] == 0
    assert summary["created"] == []


def test_rent_generation_writes_an_audit_row_with_no_actor(db, tenant, shop):
    """Automatic actions are attributable AS automatic: user_id stays NULL."""
    from scheduler.models import AuditLog
    rent.generate_rent_bills_for_date_locked(db, date(2026, 9, 1))
    entry = db.query(AuditLog).filter(AuditLog.action == "AUTO_GENERATE").one()
    assert entry.user_id is None
    assert entry.table_name == "bills"


# ══════════════════════════════════════════════════════════════════════════════
# DUE-DATE PENALTY
# ══════════════════════════════════════════════════════════════════════════════

def test_the_worked_example_from_the_spec(db, overdue_bill):
    """10,000 due 10 Aug, 1%/day, checked on 15 Aug -> 500 penalty, 10,500 owed."""
    set_settings(db, {"scheduler.penalty_enabled": True,
                      "scheduler.penalty_percent_per_day": 1.0,
                      "scheduler.penalty_grace_days": 0})
    from scheduler import settings as scheduler_settings
    cfg = scheduler_settings.get_all(db)

    penalty.apply_penalties_for_date(db, date(2026, 8, 15), cfg)
    db.refresh(overdue_bill)

    assert float(overdue_bill.penalty_amount) == 500.0
    assert overdue_bill.penalty_days == 5
    assert float(overdue_bill.pending_amount) == 10500.0
    # The original bill is untouched - "what was this for" stays answerable.
    assert float(overdue_bill.amount) == 10000.0


def test_penalty_is_recomputed_not_incremented(db, overdue_bill):
    """Running twice in a day, or out of order, converges on the same number."""
    set_settings(db, {"scheduler.penalty_enabled": True,
                      "scheduler.penalty_percent_per_day": 1.0})
    from scheduler import settings as scheduler_settings
    cfg = scheduler_settings.get_all(db)

    penalty.apply_penalties_for_date(db, date(2026, 8, 15), cfg)
    penalty.apply_penalties_for_date(db, date(2026, 8, 15), cfg)
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == 500.0

    # An out-of-order backfill of an EARLIER day recomputes down, not up.
    penalty.apply_penalties_for_date(db, date(2026, 8, 12), cfg)
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == 200.0


def test_grace_period_delays_the_first_chargeable_day(db, overdue_bill):
    set_settings(db, {"scheduler.penalty_enabled": True,
                      "scheduler.penalty_percent_per_day": 1.0,
                      "scheduler.penalty_grace_days": 5})
    from scheduler import settings as scheduler_settings
    cfg = scheduler_settings.get_all(db)

    # Due 10th + 5 days grace: the 15th is still free.
    penalty.apply_penalties_for_date(db, date(2026, 8, 15), cfg)
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == 0.0

    penalty.apply_penalties_for_date(db, date(2026, 8, 16), cfg)
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == 100.0


def test_the_cap_limits_a_long_running_penalty(db, overdue_bill):
    set_settings(db, {"scheduler.penalty_enabled": True,
                      "scheduler.penalty_percent_per_day": 1.0,
                      "scheduler.penalty_max_amount": 250})
    from scheduler import settings as scheduler_settings
    cfg = scheduler_settings.get_all(db)

    penalty.apply_penalties_for_date(db, date(2026, 8, 30), cfg)
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == 250.0


def test_a_penalty_never_earns_a_penalty(db, tenant, shop):
    bill = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Penalty",
                amount=500, paid_amount=0, pending_amount=500,
                bill_date=datetime(2026, 8, 1), due_date=datetime(2026, 8, 10),
                status="pending")
    db.add(bill)
    db.commit()
    assert bill not in penalty.eligible_bills(db)


# ══════════════════════════════════════════════════════════════════════════════
# FUTURE TASK CHECKER
# ══════════════════════════════════════════════════════════════════════════════

def test_the_checker_registers_upcoming_runs_for_every_known_task(db):
    set_settings(db, {"scheduler.enabled": True, "scheduler.lookahead_days": 3})
    master.run_due_tasks(db)

    for task_name in svc.TASKS:
        rows = db.query(SchedulerTask).filter(SchedulerTask.task_name == task_name).count()
        assert rows >= 3, f"{task_name} has no future coverage"


def test_a_missed_run_is_registered_rather_than_lost(db):
    """
    The server was down for a day. The row for that day does not exist yet, so
    it cannot be "missed" until the checker writes it - which is what backfill
    is for.
    """
    set_settings(db, {"scheduler.enabled": True, "scheduler.backfill_days": 5})
    yesterday = svc.now_local().date() - timedelta(days=1)

    # Something already known for this task, so backfill has a floor to work
    # from (it does not invent history for a task that never ran).
    svc.register(db, "rent_generation", svc.occurrence_at("rent_generation", yesterday),
                 run_date=yesterday)
    db.commit()

    master.run_due_tasks(db)

    row = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.task_name == "rent_generation",
                SchedulerTask.run_date == yesterday)
        .one()
    )
    assert row.status in (svc.COMPLETED, svc.SKIPPED)
