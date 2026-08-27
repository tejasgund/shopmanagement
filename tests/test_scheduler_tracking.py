"""
Tests for the Scheduler tracking API - /api/scheduler/*.

The API is READ-ONLY over two tables that a pair of standalone cron scripts
write. Those scripts are not importable from here on purpose (they are plain
files with their own db_config.py), so these tests write the tracking rows
directly, exactly as the scripts would, and check that what comes back out
answers the questions the dashboard has to answer:

    which day did this customer's rent get created, and was it a duplicate
    how much penalty did this bill get, and why
    what ran last night, what failed, and what did it cost

The one thing these tests deliberately do NOT do is recompute a penalty. The
`reason` column holds the explanation the script wrote on the night, in the
settings that applied then; an API that recalculated it would drift away from
what the tenant was actually charged.
"""

from datetime import date, datetime, timedelta

import pytest

from models.schema import Bill, SchedulerRun, SchedulerRunItem
from services import settings as settings_service


# ── Builders: rows exactly as the scripts write them ──────────────────────

def make_run(db, run_id, scheduler, run_date, status="SUCCESS", **kwargs):
    started = kwargs.pop("started_at", datetime(2026, 9, 7, 2, 0, 0))
    run = SchedulerRun(
        run_id=run_id, scheduler=scheduler, run_date=run_date, status=status,
        started_at=started, finished_at=started + timedelta(seconds=2),
        duration_ms=2000, trigger_source=kwargs.pop("trigger_source", "cron"),
        items_total=kwargs.pop("items_total", 0),
        items_succeeded=kwargs.pop("items_succeeded", 0),
        items_failed=kwargs.pop("items_failed", 0),
        items_skipped=kwargs.pop("items_skipped", 0),
        amount_total=kwargs.pop("amount_total", 0),
        hostname="test-host", **kwargs,
    )
    db.add(run)
    db.commit()
    return run


def make_item(db, run_id, scheduler, run_date, action, **kwargs):
    item = SchedulerRunItem(
        run_id=run_id, scheduler=scheduler, run_date=run_date, action=action,
        status=kwargs.pop("status", "SUCCESS"), **kwargs,
    )
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def rent_run(db, tenant, shop):
    """A rent run that billed one tenant and skipped one duplicate."""
    run = make_run(db, "AUTO_RENT-20260907-020015-aaaa", "auto_rent_generation",
                   date(2026, 9, 7), items_total=2, items_succeeded=1,
                   items_skipped=1, amount_total=10000)
    make_item(db, run.run_id, "auto_rent_generation", date(2026, 9, 7),
              "RENT_CREATED", user_id=tenant.id, user_name=tenant.name,
              shop_id=shop.id, shop_number=shop.shop_number, bill_id=1,
              amount=10000, period_key="RENT-2026-09",
              reason="Rent day 5 for 2026-09. 10000 monthly rent for shop A-101.")
    make_item(db, run.run_id, "auto_rent_generation", date(2026, 9, 7),
              "SKIPPED_DUPLICATE", status="SKIPPED", user_id=tenant.id,
              user_name=tenant.name, shop_id=shop.id, shop_number=shop.shop_number,
              bill_id=1, period_key="RENT-2026-09",
              reason="A Rent bill for 2026-09 already exists (bill #1).")
    return run


@pytest.fixture
def penalty_run(db, tenant, shop):
    run = make_run(db, "PENALTY-20260907-020515-bbbb", "due_bill_penalty",
                   date(2026, 9, 7), items_total=1, items_succeeded=1,
                   amount_total=500)
    make_item(db, run.run_id, "due_bill_penalty", date(2026, 9, 7),
              "PENALTY_APPLIED", user_id=tenant.id, user_name=tenant.name,
              shop_id=shop.id, shop_number=shop.shop_number, bill_id=1,
              amount=500, penalty_amount=500, penalty_days=5, penalty_rate=1.0,
              bill_due_date=datetime(2026, 8, 10),
              reason="Due 2026-08-10, 5 days overdue. 5 chargeable days x 1.0% "
                     "of the original 10000 (100/day) = 500.")
    return run


# ══════════════════════════════════════════════════════════════════════════════
# ACCESS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", [
    "/api/scheduler/summary", "/api/scheduler/runs", "/api/scheduler/items",
    "/api/scheduler/reports/daily", "/api/scheduler/reports/monthly",
    "/api/scheduler/reports/yearly", "/api/scheduler/settings",
])
def test_every_endpoint_is_admin_only(client, tenant_auth, path):
    """Billing history is not tenant-visible, however the URL is guessed."""
    assert client.get(path, headers=tenant_auth).status_code == 403


def test_there_is_no_way_to_start_a_run_through_the_api(client, admin_auth):
    """Cron owns execution. An API that could also trigger a run would mean
    two things deciding when a bill is raised."""
    for method, path in (("post", "/api/scheduler/run"),
                         ("post", "/api/scheduler/runs"),
                         ("post", "/api/bills/generate-rent")):
        assert getattr(client, method)(path, headers=admin_auth).status_code in (404, 405)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def test_summary_lists_both_schedulers_even_before_either_has_run(client, admin_auth):
    body = client.get("/api/scheduler/summary", headers=admin_auth).json()
    names = {s["scheduler"] for s in body["schedulers"]}
    assert names == {"auto_rent_generation", "due_bill_penalty"}
    assert all(s["never_run"] for s in body["schedulers"])


def test_summary_separates_last_run_from_last_SUCCESSFUL_run(
    client, admin_auth, db, tenant, shop,
):
    """A scheduler that ran an hour ago and failed looks healthy by 'last run'
    alone. That is exactly the case this screen exists to catch."""
    make_run(db, "AUTO_RENT-1", "auto_rent_generation", date(2026, 9, 1),
             status="SUCCESS", started_at=datetime(2026, 9, 1, 2, 0))
    make_run(db, "AUTO_RENT-2", "auto_rent_generation", date(2026, 9, 2),
             status="FAILED", started_at=datetime(2026, 9, 2, 2, 0),
             error_message="Cannot connect to the database")

    rent = next(s for s in client.get("/api/scheduler/summary", headers=admin_auth)
                .json()["schedulers"] if s["scheduler"] == "auto_rent_generation")

    assert rent["last_run"]["run_id"] == "AUTO_RENT-2"
    assert rent["last_run"]["status"] == "FAILED"
    assert rent["last_successful_run"]["run_id"] == "AUTO_RENT-1"
    assert rent["never_run"] is False


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def test_runs_are_filterable_by_scheduler_status_and_date(
    client, admin_auth, db, rent_run, penalty_run,
):
    get = lambda q: client.get(f"/api/scheduler/runs{q}", headers=admin_auth).json()

    assert get("")["total"] == 2
    assert get("?scheduler=auto_rent_generation")["total"] == 1
    assert get("?scheduler=due_bill_penalty")["runs"][0]["run_id"] == penalty_run.run_id
    assert get("?status=SUCCESS")["total"] == 2
    assert get("?status=FAILED")["total"] == 0
    assert get("?date_from=2026-09-07&date_to=2026-09-07")["total"] == 2
    assert get("?date_from=2026-10-01")["total"] == 0


def test_run_detail_returns_the_run_and_every_item_it_touched(
    client, admin_auth, rent_run,
):
    body = client.get(f"/api/scheduler/runs/{rent_run.run_id}", headers=admin_auth).json()

    assert body["run"]["run_id"] == rent_run.run_id
    assert body["run"]["amount_total"] == 10000.0
    assert len(body["items"]) == 2
    assert body["action_breakdown"] == {"RENT_CREATED": 1, "SKIPPED_DUPLICATE": 1}


def test_an_unknown_run_id_is_a_404_not_an_empty_run(client, admin_auth):
    assert client.get("/api/scheduler/runs/NOPE-123", headers=admin_auth).status_code == 404


def test_a_run_that_never_finished_is_flagged_as_stalled(client, admin_auth, db):
    """A killed process leaves a RUNNING row forever; it must not be mistaken
    for a run still in progress."""
    make_run(db, "AUTO_RENT-STUCK", "auto_rent_generation", date.today(),
             status="RUNNING", started_at=datetime.now() - timedelta(hours=5))
    run = client.get("/api/scheduler/runs?status=RUNNING", headers=admin_auth).json()["runs"][0]
    assert run["is_stalled"] is True


# ══════════════════════════════════════════════════════════════════════════════
# TRACKING  (rent, penalty, customer, bill)
# ══════════════════════════════════════════════════════════════════════════════

def test_rent_tracking_shows_who_was_billed_on_which_day(
    client, admin_auth, tenant, rent_run,
):
    body = client.get("/api/scheduler/items?action=RENT_CREATED",
                      headers=admin_auth).json()
    assert body["total"] == 1

    item = body["items"][0]
    assert item["user_name"] == tenant.name
    assert item["run_date"] == "2026-09-07"
    assert item["period_key"] == "RENT-2026-09"
    assert item["amount"] == 10000.0


def test_a_prevented_duplicate_is_visible_rather_than_silent(
    client, admin_auth, rent_run,
):
    """A skip that leaves no trace is indistinguishable from a tenant nobody
    looked at, and those need telling apart when a bill seems to be missing."""
    body = client.get("/api/scheduler/items?action=SKIPPED_DUPLICATE",
                      headers=admin_auth).json()
    assert body["total"] == 1
    assert "already exists" in body["items"][0]["reason"]


def test_penalty_tracking_carries_the_figures_and_the_explanation(
    client, admin_auth, penalty_run,
):
    item = client.get("/api/scheduler/items?scheduler=due_bill_penalty",
                      headers=admin_auth).json()["items"][0]

    assert item["penalty_amount"] == 500.0
    assert item["penalty_days"] == 5
    assert item["penalty_rate"] == 1.0
    # The "why", stored as written on the night - not recomputed now.
    assert "5 chargeable days" in item["reason"]
    assert "1.0%" in item["reason"]


def test_items_can_be_filtered_to_one_customer(
    client, admin_auth, db, tenant, other_tenant, rent_run,
):
    make_item(db, rent_run.run_id, "auto_rent_generation", date(2026, 9, 7),
              "RENT_CREATED", user_id=other_tenant.id, user_name=other_tenant.name,
              amount=8000, period_key="RENT-2026-09")

    mine = client.get(f"/api/scheduler/items?user_id={tenant.id}",
                      headers=admin_auth).json()
    assert mine["total"] == 2
    assert {i["user_id"] for i in mine["items"]} == {tenant.id}


def test_customer_tracking_totals_rent_and_penalty_separately(
    client, admin_auth, tenant, rent_run, penalty_run,
):
    """Rent raised and late fees charged are different kinds of money; a
    single total that mixed them would be worse than none."""
    body = client.get(f"/api/scheduler/customers/{tenant.id}", headers=admin_auth).json()

    assert body["customer"]["name"] == tenant.name
    assert body["totals"]["rent_bills_created"] == 1
    assert body["totals"]["rent_amount_total"] == 10000.0
    assert body["totals"]["penalty_amount_total"] == 500.0
    assert len(body["events"]) == 3


def test_customer_tracking_404s_for_someone_who_does_not_exist(client, admin_auth):
    assert client.get("/api/scheduler/customers/999999", headers=admin_auth).status_code == 404


def test_bill_tracking_answers_where_this_penalty_came_from(
    client, admin_auth, db, tenant, shop, penalty_run,
):
    bill = Bill(id=1, user_id=tenant.id, shop_id=shop.id, bill_type="Rent",
                amount=10000, paid_amount=0, pending_amount=10500,
                penalty_amount=500, penalty_days=5,
                bill_date=datetime(2026, 8, 1), due_date=datetime(2026, 8, 10),
                status="pending", rent_period="RENT-2026-08")
    db.add(bill)
    db.commit()

    body = client.get("/api/scheduler/bills/1", headers=admin_auth).json()

    assert body["bill"]["penalty_amount"] == 500.0
    assert body["bill"]["pending_amount"] == 10500.0
    assert len(body["history"]) == 1
    assert "5 chargeable days" in body["history"][0]["reason"]


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

def test_daily_report_keeps_rent_and_penalty_apart(
    client, admin_auth, rent_run, penalty_run,
):
    rows = client.get("/api/scheduler/reports/daily?date_from=2026-09-01&date_to=2026-09-30",
                      headers=admin_auth).json()["rows"]
    day = next(r for r in rows if r["date"] == "2026-09-07")

    assert day["rent_bills_created"] == 1
    assert day["rent_amount"] == 10000.0
    assert day["penalties_applied"] == 1
    assert day["penalty_amount"] == 500.0


def test_monthly_and_yearly_roll_the_same_numbers_up(
    client, admin_auth, rent_run, penalty_run,
):
    monthly = client.get("/api/scheduler/reports/monthly?year=2026",
                         headers=admin_auth).json()["rows"]
    september = next(r for r in monthly if r["month"] == "2026-09")
    assert september["rent_amount"] == 10000.0
    assert september["penalty_amount"] == 500.0

    yearly = client.get("/api/scheduler/reports/yearly", headers=admin_auth).json()["rows"]
    year = next(r for r in yearly if r["year"] == "2026")
    assert year["rent_amount"] == 10000.0
    assert year["penalty_amount"] == 500.0


def test_reports_count_only_what_actually_happened(
    client, admin_auth, db, tenant, rent_run,
):
    """Skipped and failed items are visible everywhere else; totalling them
    into 'rent raised' would overstate the month."""
    make_item(db, rent_run.run_id, "auto_rent_generation", date(2026, 9, 7),
              "FAILED", status="FAILED", user_id=tenant.id, amount=5000,
              error_message="storage failure")

    rows = client.get("/api/scheduler/reports/daily?date_from=2026-09-07&date_to=2026-09-07",
                      headers=admin_auth).json()["rows"]
    assert rows[0]["rent_amount"] == 10000.0     # not 15000


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def test_settings_returns_what_the_scripts_will_read_tonight(client, admin_auth):
    body = client.get("/api/scheduler/settings", headers=admin_auth).json()
    keys = set(body["values"])
    assert keys == {
        "scheduler.rent_generation_enabled", "scheduler.penalty_enabled",
        "scheduler.penalty_percent_per_day", "scheduler.penalty_grace_days",
        "scheduler.penalty_max_amount", "scheduler.penalty_on_penalty_enabled",
    }


def test_settings_can_be_updated_and_are_read_back(client, admin_auth, db):
    res = client.put("/api/scheduler/settings", headers=admin_auth, json={
        "values": {"scheduler.penalty_enabled": True,
                   "scheduler.penalty_percent_per_day": 2.5}})
    assert res.status_code == 200, res.text

    settings_service.invalidate_cache()
    values = client.get("/api/scheduler/settings", headers=admin_auth).json()["values"]
    assert values["scheduler.penalty_enabled"] is True
    assert values["scheduler.penalty_percent_per_day"] == 2.5


def test_this_endpoint_cannot_be_used_to_write_the_apps_own_settings(client, admin_auth):
    """Enforced server-side, not merely hidden in the UI - a crafted request
    must not turn the Scheduler screen into a second way in."""
    res = client.put("/api/scheduler/settings", headers=admin_auth,
                     json={"values": {"app.name": "hacked"}})
    assert res.status_code == 400
    assert "not scheduler settings" in res.json()["detail"]


def test_a_penalty_rate_that_would_be_absurd_is_refused(client, admin_auth):
    res = client.put("/api/scheduler/settings", headers=admin_auth,
                     json={"values": {"scheduler.penalty_percent_per_day": 500}})
    assert res.status_code == 400
    assert "between 0 and 100" in res.json()["detail"]


def test_the_scripts_and_the_app_agree_on_the_default_penalty_rules():
    """
    The two cron scripts are standalone: they carry their own copies of these
    defaults for a database where the key has never been customised. Two
    copies can drift, so this is the alarm.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent

    penalty_src = (root / "scheduler" / "due_bill_penalty" / "due_bill_penalty.py").read_text()
    tree = ast.parse(penalty_src)
    script_defaults = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", None) == "DEFAULTS"
    )

    assert script_defaults["enabled"] == settings_service.DEFAULTS["scheduler.penalty_enabled"]["value"]
    assert script_defaults["percent_per_day"] == settings_service.DEFAULTS["scheduler.penalty_percent_per_day"]["value"]
    assert script_defaults["grace_days"] == settings_service.DEFAULTS["scheduler.penalty_grace_days"]["value"]
    assert script_defaults["max_amount"] == settings_service.DEFAULTS["scheduler.penalty_max_amount"]["value"]
    assert script_defaults["on_penalty"] == settings_service.DEFAULTS["scheduler.penalty_on_penalty_enabled"]["value"]

    rent_src = (root / "scheduler" / "auto_rent_generation" / "auto_rent_generation.py").read_text()
    rent_tree = ast.parse(rent_src)
    rent_due_days = next(
        node.value.value
        for node in rent_tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", None) == "DEFAULT_BILL_DUE_DAYS"
    )
    assert rent_due_days == settings_service.DEFAULTS["bill.due_days"]["value"]


# ══════════════════════════════════════════════════════════════════════════════
# THE PENALTY HAS TO BE VISIBLE WHERE THE MONEY IS
#
# The scheduler writing a correct penalty is only half the job. If the screens
# that show a bill display pending_amount larger than amount with nothing
# explaining the gap, a correct charge reads as a bug - to the admin, and worse,
# to the tenant about to pay it. These lock down that the breakdown reaches
# both.
# ══════════════════════════════════════════════════════════════════════════════

def _penalised_bill(db, tenant, shop):
    """
    A bill as the penalty scheduler leaves it: the rent bill untouched at its
    own amount, and the late fee raised as a SEPARATE bill pointing back at it.

    Returns (rent_bill, fee_bill).
    """
    rent = Bill(
        user_id=tenant.id, shop_id=shop.id, bill_type="Rent",
        amount=10000, paid_amount=0, pending_amount=10000,
        bill_date=datetime(2026, 8, 1), due_date=datetime(2026, 8, 10),
        status="pending", rent_period="RENT-2026-08",
    )
    db.add(rent)
    db.commit()
    db.refresh(rent)

    fee = Bill(
        user_id=tenant.id, shop_id=shop.id, bill_type="Penalty",
        description=f"Late fee on bill #{rent.id}",
        amount=500, paid_amount=0, pending_amount=500, penalty_days=5,
        bill_date=datetime(2026, 8, 15), due_date=datetime(2026, 9, 14),
        status="pending", parent_bill_id=rent.id,
    )
    db.add(fee)
    db.commit()
    db.refresh(fee)
    return rent, fee


def test_a_rent_bill_stays_a_rent_bill(client, admin_auth, db, tenant, shop):
    """
    The whole point of the split. However long a bill is overdue, "Rent" means
    the rent - not the rent plus a fee that nobody can see the shape of.
    """
    rent, fee = _penalised_bill(db, tenant, shop)
    body = client.get(f"/api/bill/{rent.id}", headers=admin_auth).json()

    assert body["bill_type"] == "Rent"
    assert body["amount"] == 10000.0
    assert body["pending_amount"] == 10000.0     # not 10,500
    assert body["parent_bill_id"] is None


def test_the_late_fee_is_its_own_bill_pointing_at_its_parent(
    client, admin_auth, db, tenant, shop,
):
    rent, fee = _penalised_bill(db, tenant, shop)
    body = client.get(f"/api/bill/{fee.id}", headers=admin_auth).json()

    assert body["bill_type"] == "Penalty"
    assert body["amount"] == 500.0
    assert body["parent_bill_id"] == rent.id


def test_the_database_refuses_a_second_fee_for_the_same_bill(db, tenant, shop):
    """One fee bill per bill, enforced by the unique index rather than by
    whichever code path remembered to check."""
    from sqlalchemy.exc import IntegrityError

    rent, fee = _penalised_bill(db, tenant, shop)
    db.add(Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Penalty",
                amount=99, paid_amount=0, pending_amount=99,
                bill_date=datetime(2026, 8, 20), status="pending",
                parent_bill_id=rent.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_tenant_sees_the_fee_linked_to_the_bill_it_is_for(
    client, tenant_auth, db, tenant, shop,
):
    """
    Two rows, each able to explain itself: the rent bill says a fee has been
    raised against it, the fee says which bill it is for. Without the link a
    tenant sees an unexplained 'Penalty' line and rings the office.
    """
    rent, fee = _penalised_bill(db, tenant, shop)
    bills = {b["id"]: b for b in client.get("/api/tenant/bills", headers=tenant_auth).json()}

    assert bills[rent.id]["late_fee"]["bill_id"] == fee.id
    assert bills[rent.id]["late_fee"]["amount"] == 500.0
    assert bills[rent.id]["late_fee"]["days"] == 5
    assert bills[rent.id]["parent_bill"] is None

    assert bills[fee.id]["parent_bill"]["bill_id"] == rent.id
    assert bills[fee.id]["parent_bill"]["bill_type"] == "Rent"
    assert bills[fee.id]["late_fee"] is None


def test_the_tenant_home_bundle_carries_the_link_too(
    client, tenant_auth, db, tenant, shop,
):
    """The portal loads everything from /api/tenant/home on open; a link
    present only on /api/tenant/bills would never be shown."""
    rent, fee = _penalised_bill(db, tenant, shop)
    home = {b["id"]: b for b in client.get("/api/tenant/home", headers=tenant_auth).json()["bills"]}

    assert home[rent.id]["late_fee"]["bill_id"] == fee.id
    assert home[fee.id]["parent_bill"]["bill_id"] == rent.id


def test_paying_the_rent_settles_the_rent_and_leaves_only_the_fee(
    client, admin_auth, db, tenant, shop,
):
    """
    The complaint that started this. Paying the rent used to leave the bill
    'partial' with a fee that grew every day. Now the rent closes and what is
    left is a fixed, clearable fee.
    """
    rent, fee = _penalised_bill(db, tenant, shop)

    res = client.post("/api/payment", headers=admin_auth, json={
        "bill_id": rent.id, "amount": 10000.0, "payment_method": "Cash",
    })
    assert res.status_code in (200, 201), res.text

    db.refresh(rent)
    db.refresh(fee)
    assert rent.status == "paid"
    assert float(rent.pending_amount) == 0.0
    assert fee.status == "pending"
    assert float(fee.pending_amount) == 500.0


def test_a_payment_can_be_attributed_to_rent_or_to_the_fee(
    client, admin_auth, db, tenant, shop,
):
    """Impossible before the split: one bill, one payment, no way to say which
    part of it was rent and which was the late fee."""
    rent, fee = _penalised_bill(db, tenant, shop)

    client.post("/api/payment", headers=admin_auth, json={
        "bill_id": rent.id, "amount": 10000.0, "payment_method": "Cash"})
    client.post("/api/payment", headers=admin_auth, json={
        "bill_id": fee.id, "amount": 500.0, "payment_method": "UPI"})

    payments = client.get("/api/tenant/payments", headers=admin_auth).json() \
        if False else None      # tenant endpoint needs tenant auth; use the bills instead
    db.refresh(rent); db.refresh(fee)

    assert [p.amount for p in rent.payments] == [10000]
    assert [p.amount for p in fee.payments] == [500]
    assert rent.status == "paid" and fee.status == "paid"


def test_deleting_a_bill_takes_its_late_fee_with_it(
    client, admin_auth, db, tenant, shop,
):
    """A late fee for a bill that no longer exists is not collectable, and
    would sit on the tenant's screen with nothing to explain it."""
    rent, fee = _penalised_bill(db, tenant, shop)
    fee_id = fee.id

    res = client.delete(f"/api/bill/{rent.id}", headers=admin_auth)
    assert res.status_code in (200, 204), res.text

    assert db.query(Bill).filter(Bill.id == fee_id).first() is None


def test_charging_a_fee_on_a_fee_is_off_by_default():
    """
    Compounding a late fee into a late fee grows a debt faster than a tenant
    can clear it - the behaviour a penalty is meant to discourage, not cause.
    It is available for the landlords who want it, but it has to be switched
    on deliberately, and an upgrade must never switch it on for anyone.
    """
    assert settings_service.DEFAULTS["scheduler.penalty_on_penalty_enabled"]["value"] is False


def test_the_penalty_on_penalty_switch_is_editable_from_the_scheduler_screen(
    client, admin_auth,
):
    keys = {item["key"] for item in settings_service.describe_for("scheduler")}
    assert "scheduler.penalty_on_penalty_enabled" in keys

    res = client.put("/api/scheduler/settings", headers=admin_auth,
                     json={"values": {"scheduler.penalty_on_penalty_enabled": True}})
    assert res.status_code == 200, res.text

    settings_service.invalidate_cache()
    values = client.get("/api/scheduler/settings", headers=admin_auth).json()["values"]
    assert values["scheduler.penalty_on_penalty_enabled"] is True
