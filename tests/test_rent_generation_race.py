"""
Regression tests for the duplicate rent bill bug (2026-08-13).

What happened: uvicorn runs with --workers 2, and the scheduler is started in
the FastAPI startup hook, so each worker had its own APScheduler firing the
same cron. Both fired at 15:52:00, each on its own DB session. Neither could
see the other's uncommitted row, so both read "no rent bill for August yet"
and both inserted — shop 10 got bills 120 and 121.

These tests hold two simulated workers at a barrier just before they commit,
which forces that exact ordering deterministically instead of relying on
thread timing.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest

import app as app_module
from create_tables import Bill, Shop, User, UserShop
from db_config import SessionLocal


TARGET = date(2026, 8, 13)


@pytest.fixture
def rent_tenant(db):
    """A tenant opted into auto rent billing on the 13th, with one shop."""
    user = User(
        name="Tenant Six", mobile="9000000006",
        password_hash="x", role="tenant", is_active=True,
        auto_rent_bill_enabled=True, rent_bill_date=TARGET.day,
    )
    db.add(user)
    shop = Shop(shop_number="S-10", status="occupied",
                shop_rent=Decimal("10000"), shop_deposit=Decimal("0"))
    db.add(shop)
    db.commit()
    db.add(UserShop(user_id=user.id, shop_id=shop.id))
    db.commit()
    return user


def _run_two_workers(fn, barrier_timeout=2):
    """
    Run `fn` twice at once, each on its own session, both held at a barrier
    just before committing — the production interleaving.
    Returns (results, rent_bill_count).
    """
    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(idx):
        session = SessionLocal()
        real_commit = session.commit
        fired = {"done": False}

        def commit_after_barrier():
            if not fired["done"]:
                fired["done"] = True
                try:
                    barrier.wait(timeout=barrier_timeout)
                except threading.BrokenBarrierError:
                    pass          # the other worker was locked out - expected
            return real_commit()

        session.commit = commit_after_barrier
        try:
            results[idx] = fn(session, TARGET)
        except Exception as exc:                      # noqa: BLE001
            results[idx] = {"error": str(exc)}
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = SessionLocal()
    try:
        count = check.query(Bill).filter(Bill.bill_type == "Rent").count()
    finally:
        check.close()
    return results, count


def test_unlocked_generation_can_double_bill(db, rent_tenant):
    """
    Documents the bug itself: without the lock, two simultaneous runs each
    insert a bill. If this ever stops being true the lock may be redundant —
    but until then it proves the lock is load-bearing.
    """
    _, count = _run_two_workers(app_module.generate_rent_bills_for_date)
    assert count == 2, "expected the unguarded race to produce a duplicate"


def test_locked_generation_creates_exactly_one_bill(db, rent_tenant):
    """The fix: two simultaneous runs produce one bill, not two."""
    results, count = _run_two_workers(app_module.generate_rent_bills_for_date_locked)
    assert count == 1, f"expected exactly one rent bill, got {count}"

    # One run created it; the other found it already there (or was locked out).
    created = [len(r.get("created", [])) for r in results if isinstance(r, dict)]
    assert sorted(created) == [0, 1]


def test_locked_generation_is_still_idempotent_when_run_twice(db, rent_tenant):
    """Running it again later must not add a second bill for the same month."""
    first = app_module.generate_rent_bills_for_date_locked(db, TARGET)
    assert len(first["created"]) == 1

    second = app_module.generate_rent_bills_for_date_locked(db, TARGET)
    assert second["created"] == []
    assert second["skipped_existing"] == 1

    assert db.query(Bill).filter(Bill.bill_type == "Rent").count() == 1


def test_manual_endpoint_is_locked_too(client, admin_auth, db, rent_tenant):
    """
    The admin "Generate rent bills" button goes through the same lock, so a
    double-click can't double-bill either.
    """
    resp = client.post(f"/api/bills/generate-rent?date={TARGET.isoformat()}", headers=admin_auth)
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 1

    again = client.post(f"/api/bills/generate-rent?date={TARGET.isoformat()}", headers=admin_auth)
    assert again.json()["created"] == []

    db.expire_all()
    assert db.query(Bill).filter(Bill.bill_type == "Rent").count() == 1


def test_lock_is_released_after_a_failed_run(db, rent_tenant, monkeypatch):
    """
    If generation blows up, the lock must still be released — otherwise every
    later run would be blocked until the process restarted.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(app_module, "generate_rent_bills_for_date", boom)
    with pytest.raises(RuntimeError):
        app_module.generate_rent_bills_for_date_locked(db, TARGET)
    monkeypatch.undo()

    # The lock is free again, so a normal run works.
    result = app_module.generate_rent_bills_for_date_locked(db, TARGET)
    assert len(result["created"]) == 1


def test_scheduler_can_be_switched_off_per_process(monkeypatch):
    """
    RUN_SCHEDULER=0 stops a worker starting its own scheduler, so one process
    can own the cron instead of every worker duplicating it.
    """
    started = {"yes": False}
    monkeypatch.setattr(app_module.scheduler, "start",
                        lambda *a, **k: started.__setitem__("yes", True))

    monkeypatch.setenv("RUN_SCHEDULER", "0")
    app_module._start_rent_bill_scheduler()
    assert started["yes"] is False, "scheduler should not start when RUN_SCHEDULER=0"
