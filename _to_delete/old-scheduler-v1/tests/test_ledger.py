"""
The ledger and the coordinator: what was supposed to happen, what did, and
what the scheduler does when something goes wrong.
"""

from datetime import timedelta

import pytest

from scheduler import master
from scheduler import service as svc
from scheduler.models import SchedulerTask
from .conftest import set_settings


def test_registering_the_same_occurrence_twice_is_a_no_op(db):
    when = svc.occurrence_at("rent_generation", svc.now_local().date())
    assert svc.register(db, "rent_generation", when) is not None
    db.commit()
    assert svc.register(db, "rent_generation", when) is None
    assert db.query(SchedulerTask).count() == 1


def test_an_empty_ledger_bootstraps_itself(db):
    """A fresh install has nothing scheduled, and nothing to schedule it -
    without the bootstrap it would stay empty forever."""
    assert db.query(SchedulerTask).count() == 0
    assert svc.ensure_bootstrapped(db) is True
    assert db.query(SchedulerTask).count() == 1
    # Already covered now, so it does not queue another on every sweep.
    assert svc.ensure_bootstrapped(db) is False


def test_the_master_switch_records_skips_rather_than_letting_work_pile_up(db):
    """
    Off does not mean invisible. Cron still fired and the sweep still ran; what
    stops is the WORK - and every due occurrence is closed off as SKIPPED with
    the reason recorded, rather than left as a silently growing pile of pending
    rows nobody is looking at.
    """
    set_settings(db, {"scheduler.enabled": False})

    # Occurrences that were already due before the switch was turned off.
    earlier = svc.now_local() - timedelta(hours=1)
    for task_name in svc.TASKS:
        svc.register(db, task_name, earlier, run_date=earlier.date())
    db.commit()

    summary = master.run_due_tasks(db)

    assert summary["scheduler_enabled"] is False
    assert summary["tasks_skipped"] == len(svc.TASKS)
    assert summary["tasks_run"] == 0
    assert db.query(SchedulerTask).filter(SchedulerTask.status == svc.PENDING).count() == 0

    for row in db.query(SchedulerTask).all():
        assert row.status == svc.SKIPPED
        assert "master scheduler is disabled" in row.skip_reason


def test_a_disabled_task_is_skipped_with_the_reason_recorded(db):
    set_settings(db, {"scheduler.enabled": True, "scheduler.penalty_enabled": False})
    today = svc.now_local().date()
    svc.register(db, "due_date_penalty", svc.occurrence_at("due_date_penalty", today),
                 run_date=today)
    db.commit()

    master.run_due_tasks(db, only_task="due_date_penalty")
    row = db.query(SchedulerTask).filter(
        SchedulerTask.task_name == "due_date_penalty", SchedulerTask.run_date == today
    ).one()
    assert row.status == svc.SKIPPED
    assert "disabled" in row.skip_reason


def test_one_failing_task_does_not_stop_the_others(db, tenant, shop, monkeypatch):
    """Failure isolation is the coordinator's whole reason for existing."""
    import scheduler.tasks.due_date_penalty as ddp
    monkeypatch.setattr(ddp, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    set_settings(db, {"scheduler.enabled": True,
                      "scheduler.rent_generation_enabled": True,
                      "scheduler.penalty_enabled": True})
    today = svc.now_local().date()
    for task_name in svc.TASKS:
        svc.register(db, task_name, svc.occurrence_at(task_name, today), run_date=today)
    db.commit()

    summary = master.run_due_tasks(db)

    assert summary["tasks_failed"] == 1
    assert summary["tasks_run"] >= 1              # the others still got their turn
    failed = db.query(SchedulerTask).filter(SchedulerTask.status == svc.FAILED).one()
    assert failed.task_name == "due_date_penalty"
    assert "boom" in failed.error_message
    assert "Traceback" in failed.error_message


def test_a_failed_run_stays_failed_until_someone_retries_it(db, monkeypatch):
    """An automatic retry would hide a persistent fault behind an attempt count."""
    import scheduler.tasks.rent_generation as rent_task
    monkeypatch.setattr(rent_task, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))

    set_settings(db, {"scheduler.enabled": True, "scheduler.rent_generation_enabled": True})
    today = svc.now_local().date()
    svc.register(db, "rent_generation", svc.occurrence_at("rent_generation", today),
                 run_date=today)
    db.commit()

    master.run_due_tasks(db, only_task="rent_generation")
    master.run_due_tasks(db, only_task="rent_generation")

    row = db.query(SchedulerTask).filter(
        SchedulerTask.task_name == "rent_generation", SchedulerTask.run_date == today
    ).one()
    assert row.status == svc.FAILED
    assert row.attempts == 1                       # not retried behind anyone's back

    svc.retry(db, row)
    assert row.status == svc.PENDING
    assert row.attempts == 1                       # history is kept


def test_a_run_that_never_happened_shows_as_missed_not_as_an_absence(db):
    long_ago = svc.now_local() - svc.MISSED_AFTER - timedelta(minutes=5)
    row = svc.register(db, "rent_generation", long_ago, run_date=long_ago.date())
    db.commit()
    assert svc.is_missed(row) is True


def test_an_abandoned_run_shows_as_stuck(db):
    """A killed process leaves a RUNNING row that would otherwise block its
    next occurrence forever."""
    row = svc.register(db, "rent_generation", svc.now_local())
    db.commit()
    svc.claim(db, row)
    row.started_at = svc.now_local() - svc.STUCK_AFTER - timedelta(minutes=1)
    db.commit()
    assert svc.is_stuck(row) is True


def test_a_task_removed_from_the_build_is_closed_off_not_left_pending(db):
    set_settings(db, {"scheduler.enabled": True})
    row = svc.register(db, "task_that_no_longer_exists", svc.now_local())
    db.commit()
    from scheduler import settings as scheduler_settings
    svc.execute(db, row, scheduler_settings.get_all(db))
    assert row.status == svc.SKIPPED
    assert "no longer exists" in row.skip_reason


def test_every_registered_task_has_a_resolvable_runner():
    """A typo in the registry should fail here, not at 2am."""
    for task_name, spec in svc.TASKS.items():
        runner = svc._resolve(spec["runner"])
        assert callable(runner), f"{task_name} has no runnable runner"
