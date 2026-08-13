"""
Proof of the duplicate-rent-bill race.   python _race_demo.py

Reproduces what happened on 2026-08-13: uvicorn runs with --workers 2, every
worker starts its own APScheduler, so the same cron fired twice at the same
second. Each run has its own DB session, so neither sees the other's
uncommitted row — both read "no rent bill yet" and both insert.

To make that deterministic rather than a coin toss, each simulated worker is
held at a barrier just before it commits. That forces the exact production
ordering: both reads happen before either write lands.

    read A ─┐
    read B ─┤ (both see "no bill")
            ├─ barrier
    write A ┘
    write B     -> two bills
"""

import os
import threading

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/race_demo.db")

from datetime import date                                    # noqa: E402
from decimal import Decimal                                  # noqa: E402

from db_config import Base, SessionLocal, engine             # noqa: E402
from create_tables import Bill, Shop, User, UserShop, hash_password  # noqa: E402
import app as A                                              # noqa: E402


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    u = User(name="Tenant Six", mobile="9000000006",
             password_hash=hash_password("x"), role="tenant",
             is_active=True, auto_rent_bill_enabled=True, rent_bill_date=13)
    db.add(u)
    s = Shop(shop_number="S-10", status="occupied",
             shop_rent=Decimal("10000"), shop_deposit=Decimal("0"))
    db.add(s)
    db.commit()
    db.add(UserShop(user_id=u.id, shop_id=s.id))
    db.commit()
    db.close()


def run(fn, target, results, idx, barrier):
    db = SessionLocal()

    # Hold this worker just before it commits, so both workers have already
    # done their "does a bill exist?" read. A timeout keeps it from deadlocking
    # when the lock genuinely serialises the two (then only one ever arrives).
    real_commit = db.commit
    fired = {"done": False}

    def commit_after_barrier():
        if not fired["done"]:
            fired["done"] = True
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass          # the other worker never got here - it was locked out
        return real_commit()

    db.commit = commit_after_barrier

    try:
        results[idx] = fn(db, target)
    except Exception as exc:                     # noqa: BLE001
        results[idx] = {"error": str(exc)}
    finally:
        db.close()


def race(fn, label):
    seed()
    target = date(2026, 8, 13)
    results = [None, None]
    barrier = threading.Barrier(2)

    threads = [threading.Thread(target=run, args=(fn, target, results, i, barrier))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db = SessionLocal()
    bills = db.query(Bill).filter(Bill.bill_type == "Rent").all()
    db.close()

    print(f"\n--- {label} ---")
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            print(f"  worker {i + 1}: {r}")
            continue
        note = " (locked out — correct)" if r.get("skipped_locked") else ""
        print(f"  worker {i + 1}: created={r.get('created')} "
              f"skipped_existing={r.get('skipped_existing')}{note}")
    verdict = "DUPLICATE BUG" if len(bills) > 1 else "correct — one bill"
    print(f"  rent bills in DB: {len(bills)}  ->  {verdict}")
    return len(bills)


if __name__ == "__main__":
    print(f"engine: {engine.dialect.name}")
    unlocked = race(A.generate_rent_bills_for_date, "WITHOUT the lock (what shipped)")
    locked = race(A.generate_rent_bills_for_date_locked, "WITH the lock (the fix)")

    print("\n" + "=" * 46)
    print(f"  unlocked -> {unlocked} bill(s)   {'<-- the production bug' if unlocked > 1 else ''}")
    print(f"  locked   -> {locked} bill(s)   {'<-- fixed' if locked == 1 else ''}")
    print("=" * 46)

    if engine.dialect.name != "mysql":
        print("\nThis run proves the in-process layer (threads). The cross-process")
        print("layer is the MySQL named lock — point DATABASE_URL at MySQL and")
        print("run two copies of this script at once to exercise that one.")

    raise SystemExit(0 if (unlocked > 1 and locked == 1) else 1)
