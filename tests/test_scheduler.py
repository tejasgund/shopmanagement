"""
The scheduler: settings, task ledger, the two business tasks, and recovery.

Organised by the guarantee each group defends. The one that matters most is
"nothing is silently missed": a run that never happened must end up visible
and processed, not merely absent.
"""

from datetime import date, datetime, timedelta

import pytest

from create_tables import Bill, SchedulerTask, Shop, User, UserShop, hash_password
import penalty_billing
import settings_service
from scheduler import master as scheduler_master
from scheduler import service as svc


def _set(db, **values):
    settings_service.set_many(db, values)
    db.commit()
    settings_service.invalidate_cache()


@pytest.fixture
def rent_tenant(db):
    """A tenant due a rent bill today, so a sweep has real work to do."""
    today = svc.now_local().day
    u = User(name="Rent Tenant", mobile="9000004444", email="rt@test.com",
             password_hash=hash_password("x"), role="tenant", is_active=True,
             rent_bill_date=today, auto_rent_bill_enabled=True)
    db.add(u); db.commit(); db.refresh(u)
    s = Shop(shop_number="R-1", status="occupied", shop_rent=10000, shop_deposit=0)
    db.add(s); db.commit(); db.refresh(s)
    db.add(UserShop(user_id=u.id, shop_id=s.id)); db.commit()
    return u


@pytest.fixture
def overdue_bill(db, tenant, shop):
    """The spec's example: 10,000 due on the 10th."""
    b = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Rent", amount=10000,
             paid_amount=0, pending_amount=10000, status="pending",
             bill_date=datetime(2026, 8, 1), due_date=datetime(2026, 8, 10))
    db.add(b); db.commit(); db.refresh(b)
    return b


# ══════════════════════════════════════════════════════════════════════════════
# PENALTY RULES
# ══════════════════════════════════════════════════════════════════════════════

def test_penalty_matches_the_specified_example(db, overdue_bill):
    """10,000 due 10 Aug, 5 days overdue, 1%/day -> 500 penalty, 10,500 payable."""
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0,
                "scheduler.penalty_max_amount": 0})
    q = penalty_billing.quote(overdue_bill, settings_service.get_all(db), date(2026, 8, 15))

    assert q["original_amount"] == 10000.0
    assert q["days_overdue"] == 5
    assert q["penalty_per_day"] == 100.0
    assert q["total_penalty"] == 500.0
    assert q["total_payable"] == 10500.0


def test_the_original_amount_is_never_touched(db, overdue_bill):
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0})
    penalty_billing.apply_penalties_for_date(db, date(2026, 8, 20))
    db.refresh(overdue_bill)

    assert float(overdue_bill.amount) == 10000.0, "penalty leaked into the original amount"
    assert float(overdue_bill.penalty_amount) == 1000.0
    assert float(overdue_bill.pending_amount) == 11000.0


def test_running_the_penalty_task_repeatedly_changes_nothing(db, overdue_bill):
    """Idempotency: the penalty is recomputed, never incremented."""
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0})
    for _ in range(5):
        penalty_billing.apply_penalties_for_date(db, date(2026, 8, 15))
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == 500.0


def test_a_paid_bill_stops_accruing(db, overdue_bill):
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0})
    penalty_billing.apply_penalties_for_date(db, date(2026, 8, 15))
    db.refresh(overdue_bill)
    charged = float(overdue_bill.penalty_amount)

    overdue_bill.status = "paid"
    db.commit()
    penalty_billing.apply_penalties_for_date(db, date(2026, 9, 30))
    db.refresh(overdue_bill)
    assert float(overdue_bill.penalty_amount) == charged


def test_grace_period_delays_the_first_charge(db, overdue_bill):
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 5})
    cfg = settings_service.get_all(db)

    assert penalty_billing.quote(overdue_bill, cfg, date(2026, 8, 15))["total_penalty"] == 0.0
    q = penalty_billing.quote(overdue_bill, cfg, date(2026, 8, 20))
    assert q["days_overdue"] == 10 and q["chargeable_days"] == 5
    assert q["total_penalty"] == 500.0


def test_maximum_penalty_caps_the_total(db, overdue_bill):
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0,
                "scheduler.penalty_max_amount": 250})
    q = penalty_billing.quote(overdue_bill, settings_service.get_all(db), date(2026, 9, 30))
    assert q["total_penalty"] == 250.0 and q["capped"] is True


def test_a_penalty_never_earns_a_penalty(db, tenant, shop):
    b = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Penalty", amount=500,
             paid_amount=0, pending_amount=500, status="pending",
             bill_date=datetime(2026, 8, 1), due_date=datetime(2026, 8, 2))
    db.add(b); db.commit(); db.refresh(b)
    _set(db, **{"scheduler.penalty_percent_per_day": 5.0})

    penalty_billing.apply_penalties_for_date(db, date(2026, 9, 1))
    db.refresh(b)
    assert float(b.penalty_amount) == 0.0


def test_a_penalty_is_payable_through_the_normal_payment_route(
    db, client, admin_auth, overdue_bill
):
    """
    Reconciliation is one function, so the penalty is owed everywhere at once -
    including to a tenant paying the balance off online.
    """
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0})
    penalty_billing.apply_penalties_for_date(db, date(2026, 8, 15))
    db.refresh(overdue_bill)
    assert float(overdue_bill.pending_amount) == 10500.0

    # Paying only the original leaves the penalty outstanding, not the bill paid.
    client.post("/api/payment", headers=admin_auth, json={
        "bill_id": overdue_bill.id, "amount": 10000, "payment_method": "Cash",
    })
    db.expire_all(); db.refresh(overdue_bill)
    assert overdue_bill.status == "partial"
    assert float(overdue_bill.pending_amount) == 500.0

    client.post("/api/payment", headers=admin_auth, json={
        "bill_id": overdue_bill.id, "amount": 500, "payment_method": "Cash",
    })
    db.expire_all(); db.refresh(overdue_bill)
    assert overdue_bill.status == "paid"


# ══════════════════════════════════════════════════════════════════════════════
# THE LEDGER AND THE MASTER SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def test_an_empty_ledger_bootstraps_itself(db, rent_tenant):
    assert db.query(SchedulerTask).count() == 0
    scheduler_master.run_due_tasks(db)
    assert db.query(SchedulerTask).count() > 0


def test_a_sweep_creates_no_duplicate_bills_however_often_it_runs(db, rent_tenant):
    scheduler_master.run_due_tasks(db)
    bills = db.query(Bill).count()
    for _ in range(3):
        scheduler_master.run_due_tasks(db)
    assert db.query(Bill).count() == bills


def test_registering_the_same_occurrence_twice_is_a_no_op(db):
    when = svc.now_local()
    assert svc.register(db, "rent_generation", when) is not None
    assert svc.register(db, "rent_generation", when) is None
    assert db.query(SchedulerTask).filter(SchedulerTask.scheduled_for == when).count() == 1


def test_a_run_that_never_happened_is_visible_and_then_recovered(db, rent_tenant):
    """
    The central guarantee. A rent run for three days ago that never executed
    must read as missed, and must be picked up once the scheduler is back -
    not quietly forgotten.
    """
    missed_day = svc.now_local().date() - timedelta(days=3)
    when = svc.occurrence_at("rent_generation", missed_day)
    svc.register(db, "rent_generation", when, run_date=missed_day)
    db.commit()

    row = db.query(SchedulerTask).filter(SchedulerTask.scheduled_for == when).one()
    assert row.status == svc.PENDING
    assert svc.is_missed(row) is True

    scheduler_master.run_due_tasks(db)
    db.refresh(row)
    assert row.status == svc.COMPLETED


def test_the_master_switch_stops_work_but_records_what_it_stopped(db, rent_tenant):
    """Cron still runs; the tasks do not - and each one says so."""
    _set(db, **{"scheduler.enabled": False})
    due = svc.now_local() - timedelta(minutes=5)
    svc.register(db, "rent_generation", due, run_date=due.date())
    db.commit()

    summary = scheduler_master.run_due_tasks(db)
    assert summary["scheduler_enabled"] is False
    assert summary["tasks_run"] == 0
    assert summary["tasks_skipped"] >= 1

    row = db.query(SchedulerTask).filter(SchedulerTask.scheduled_for == due).one()
    assert row.status == svc.SKIPPED
    assert "disabled" in (row.skip_reason or "").lower()
    assert db.query(Bill).count() == 0


def test_a_disabled_task_is_skipped_while_the_others_still_run(db, rent_tenant):
    _set(db, **{"scheduler.enabled": True, "scheduler.penalty_enabled": False})
    day = svc.now_local().date() - timedelta(days=1)
    svc.register(db, "due_date_penalty", svc.occurrence_at("due_date_penalty", day), run_date=day)
    svc.register(db, "rent_generation", svc.occurrence_at("rent_generation", day), run_date=day)
    db.commit()

    summary = scheduler_master.run_due_tasks(db)
    assert "due_date_penalty" in {t["task_name"] for t in summary["skipped"]}
    assert "rent_generation" in {t["task_name"] for t in summary["executed"]}


def test_one_failing_task_does_not_stop_the_others(db, rent_tenant, monkeypatch):
    """Failure isolation, and the failure is kept with its error."""
    _set(db, **{"scheduler.enabled": True, "scheduler.penalty_enabled": True})
    import scheduler.tasks.due_date_penalty as ddp

    def boom(*_args, **_kwargs):
        raise RuntimeError("Database connection timeout")

    monkeypatch.setattr(ddp, "run", boom)

    day = svc.now_local().date() - timedelta(days=1)
    svc.register(db, "due_date_penalty", svc.occurrence_at("due_date_penalty", day), run_date=day)
    svc.register(db, "rent_generation", svc.occurrence_at("rent_generation", day), run_date=day)
    db.commit()

    summary = scheduler_master.run_due_tasks(db)
    assert "due_date_penalty" in {t["task_name"] for t in summary["failed"]}
    assert "rent_generation" in {t["task_name"] for t in summary["executed"]}

    failed = db.query(SchedulerTask).filter(SchedulerTask.status == svc.FAILED).first()
    assert "Database connection timeout" in failed.error_message
    assert failed.attempts == 1


def test_a_failed_task_can_be_retried_and_keeps_its_attempt_count(db, rent_tenant, monkeypatch):
    import scheduler.tasks.due_date_penalty as ddp
    monkeypatch.setattr(ddp, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    day = svc.now_local().date() - timedelta(days=1)
    svc.register(db, "due_date_penalty", svc.occurrence_at("due_date_penalty", day), run_date=day)
    db.commit()
    _set(db, **{"scheduler.penalty_enabled": True})
    scheduler_master.run_due_tasks(db)

    # The checker may have backfilled more than one occurrence of this task;
    # any one of the failures is enough to exercise retry.
    row = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.status == svc.FAILED,
                SchedulerTask.task_name == "due_date_penalty")
        .order_by(SchedulerTask.id.asc())
        .first()
    )
    assert row is not None
    monkeypatch.undo()

    svc.retry(db, row)
    db.refresh(row)
    assert row.status == svc.PENDING and row.attempts == 1

    scheduler_master.run_due_tasks(db)
    db.refresh(row)
    assert row.status == svc.COMPLETED and row.attempts == 2


def test_a_new_task_registers_itself_with_no_frontend_change(db, rent_tenant):
    """Adding a registry entry is the whole change - nothing else lists tasks."""
    scheduler_master.run_due_tasks(db)
    svc.TASKS["demo_task"] = {
        "label": "Demo", "description": "d", "enable_setting": None,
        "run_at": datetime(2026, 1, 1, 3, 30).time(),
        "runner": "scheduler.tasks.future_task_checker:run",
    }
    try:
        scheduler_master.run_due_tasks(db)
        assert db.query(SchedulerTask).filter(SchedulerTask.task_name == "demo_task").count() > 0
    finally:
        svc.TASKS.pop("demo_task", None)


# ══════════════════════════════════════════════════════════════════════════════
# THE MONITORING API
# ══════════════════════════════════════════════════════════════════════════════

def test_status_endpoint_reports_the_switch_and_the_counts(client, admin_auth, db, rent_tenant):
    scheduler_master.run_due_tasks(db)
    body = client.get("/api/scheduler/status", headers=admin_auth).json()

    assert body["scheduler_enabled"] is True
    assert {t["task_name"] for t in body["tasks"]} >= {
        "rent_generation", "due_date_penalty", "future_task_checker"
    }
    assert "counts" in body and "penalty" in body


def test_task_views_are_all_reachable(client, admin_auth, db, rent_tenant):
    scheduler_master.run_due_tasks(db)
    for view in ("running", "upcoming", "completed", "failed", "missed", "skipped", "history"):
        resp = client.get(f"/api/scheduler/tasks?view={view}", headers=admin_auth)
        assert resp.status_code == 200, f"{view}: {resp.text}"
        assert isinstance(resp.json(), list)


def test_a_completed_task_cannot_be_retried_away(client, admin_auth, db, rent_tenant):
    """Retrying a good run would overwrite the record of what happened."""
    scheduler_master.run_due_tasks(db)
    done = db.query(SchedulerTask).filter(SchedulerTask.status == svc.COMPLETED).first()
    resp = client.post(f"/api/scheduler/tasks/{done.id}/retry", headers=admin_auth)
    assert resp.status_code == 400


def test_the_scheduler_api_is_admin_only(client, tenant_auth):
    assert client.get("/api/scheduler/status", headers=tenant_auth).status_code == 403
    assert client.get("/api/scheduler/tasks", headers=tenant_auth).status_code == 403
    assert client.post("/api/scheduler/run", headers=tenant_auth).status_code == 403


def test_run_now_uses_the_same_coordinator_as_cron(client, admin_auth, db, rent_tenant):
    body = client.post("/api/scheduler/run", headers=admin_auth).json()
    assert "tasks_run" in body and "scheduler_enabled" in body
    assert db.query(SchedulerTask).count() > 0


def test_tenant_bills_explain_the_penalty(client, tenant_auth, db, overdue_bill):
    """The tenant must be able to see why the amount went up."""
    _set(db, **{"scheduler.penalty_percent_per_day": 1.0, "scheduler.penalty_grace_days": 0})
    penalty_billing.apply_penalties_for_date(db, date(2026, 8, 15))

    bills = client.get("/api/tenant/bills", headers=tenant_auth).json()
    bill = next(b for b in bills if b["id"] == overdue_bill.id)

    assert bill["amount"] == 10000.0                     # original, unchanged
    assert bill["penalty"]["penalty_amount"] == 500.0
    assert bill["penalty"]["total_payable"] == 10500.0
    assert bill["penalty"]["has_penalty"] is True


# ══════════════════════════════════════════════════════════════════════════════
# APP SEPARATION
#
# Scheduler settings belong to the Scheduler app. They share the settings
# table - one mechanism, one audit trail - but the two apps cannot reach into
# each other's configuration, and that is enforced by the API rather than by
# each UI choosing to behave.
# ══════════════════════════════════════════════════════════════════════════════

def test_the_main_settings_screen_does_not_show_scheduler_settings(client, admin_auth):
    rows = client.get("/api/settings", headers=admin_auth).json()["settings"]
    leaked = [r["key"] for r in rows if r["key"].startswith("scheduler.")]
    assert leaked == [], f"scheduler settings leaked into the main app: {leaked}"


def test_the_main_settings_endpoint_refuses_to_write_scheduler_settings(client, admin_auth):
    """Refused, not silently dropped - a dropped key looks like a save that worked."""
    resp = client.put("/api/settings", headers=admin_auth,
                      json={"values": {"scheduler.enabled": False}})
    assert resp.status_code == 400
    assert "Scheduler app" in resp.json()["detail"]


def test_reset_all_settings_leaves_the_scheduler_alone(client, admin_auth, db):
    """A reset in the main app must not wipe the scheduler's configuration."""
    _set(db, **{"scheduler.penalty_percent_per_day": 2.5})

    resp = client.post("/api/settings/reset", headers=admin_auth)
    assert resp.status_code == 200
    assert not any(k.startswith("scheduler.") for k in resp.json()["reset"])

    settings_service.invalidate_cache()
    assert settings_service.get_all(db)["scheduler.penalty_percent_per_day"] == 2.5


def test_the_scheduler_app_serves_its_own_settings(client, admin_auth):
    body = client.get("/api/scheduler/settings", headers=admin_auth).json()
    keys = {r["key"] for r in body["settings"]}

    assert keys >= {"scheduler.enabled", "scheduler.penalty_percent_per_day"}
    assert all(k.startswith("scheduler.") for k in keys), "main app settings leaked in"


def test_the_scheduler_app_cannot_change_the_main_applications_settings(client, admin_auth):
    """The mirror image - the boundary is refused in both directions."""
    resp = client.put("/api/scheduler/settings", headers=admin_auth,
                      json={"values": {"app.name": "Hijacked"}})
    assert resp.status_code == 400
    assert "Only scheduler settings" in resp.json()["detail"]


def test_scheduler_settings_save_and_take_effect(client, admin_auth, db):
    resp = client.put("/api/scheduler/settings", headers=admin_auth,
                      json={"values": {"scheduler.penalty_percent_per_day": 1.5,
                                       "scheduler.penalty_grace_days": 3}})
    assert resp.status_code == 200, resp.text

    settings_service.invalidate_cache()
    cfg = settings_service.get_all(db)
    assert cfg["scheduler.penalty_percent_per_day"] == 1.5
    assert cfg["scheduler.penalty_grace_days"] == 3


def test_scheduler_settings_are_validated(client, admin_auth):
    resp = client.put("/api/scheduler/settings", headers=admin_auth,
                      json={"values": {"scheduler.penalty_percent_per_day": 500}})
    assert resp.status_code == 400
    assert "between 0 and 100" in resp.json()["detail"]


def test_scheduler_settings_are_admin_only(client, tenant_auth):
    assert client.get("/api/scheduler/settings", headers=tenant_auth).status_code == 403
    assert client.put("/api/scheduler/settings", headers=tenant_auth,
                      json={"values": {"scheduler.enabled": False}}).status_code == 403
