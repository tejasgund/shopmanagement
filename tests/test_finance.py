"""
Finance section: month register, tenant ledger, defaulters.

Scenario used throughout (shop A-101 rent 10,000, tenant "Tenant One"):
    Oct 2026  Rent 10,000          paid 4,000 on 05-Nov  -> 6,000 still owed
    Nov 2026  Rent 10,000          paid 5,000 on 10-Nov
              Electricity 1,240    unpaid
Second tenant (B-202) has no bill in November at all.
"""

from datetime import datetime

import pytest

from models.schema import Bill, UserShop


def _bill(client, auth, user, shop, bill_type, when, amount=None, due=None):
    body = {"user_id": user.id, "shop_id": shop.id, "bill_type": bill_type,
            "bill_date": when.isoformat()}
    if amount is not None:
        body["amount"] = amount
    if due is not None:
        body["due_date"] = due.isoformat()
    res = client.post("/api/bill", headers=auth, json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _pay(client, auth, bill_id, amount, when):
    res = client.post("/api/payment", headers=auth, json={
        "bill_id": bill_id, "amount": amount, "payment_method": "Cash",
        "payment_date": when.isoformat()})
    assert res.status_code == 201, res.text
    return res.json()["id"]


@pytest.fixture
def scenario(client, admin_auth, tenant, shop, other_tenant, other_shop):
    oct_rent = _bill(client, admin_auth, tenant, shop, "Rent", datetime(2026, 10, 1), due=datetime(2026, 10, 10))
    nov_rent = _bill(client, admin_auth, tenant, shop, "Rent", datetime(2026, 11, 1), due=datetime(2026, 11, 10))
    nov_elec = _bill(client, admin_auth, tenant, shop, "Electricity", datetime(2026, 11, 3), amount=1240,
                     due=datetime(2026, 11, 20))
    _pay(client, admin_auth, oct_rent, 4000, datetime(2026, 11, 5))
    _pay(client, admin_auth, nov_rent, 5000, datetime(2026, 11, 10))
    return {"oct_rent": oct_rent, "nov_rent": nov_rent, "nov_elec": nov_elec}


def _register(client, auth, **params):
    q = "&".join(f"{k}={v}" for k, v in {"year": 2026, "month": 11, **params}.items())
    res = client.get(f"/api/finance/register?{q}", headers=auth)
    assert res.status_code == 200, res.text
    return res.json()


# ── register ──────────────────────────────────────────────────────────────

def test_register_row_figures(client, admin_auth, tenant, shop, scenario):
    data = _register(client, admin_auth)
    row = next(r for r in data["rows"] if r["user_id"] == tenant.id)
    assert row["shop_number"] == "A-101"
    assert row["rent"] == 10000 and row["electricity"] == 1240
    assert row["billed"] == 11240
    assert row["paid"] == 5000                 # against November's bills
    assert row["due"] == 6240
    assert row["older_due"] == 6000            # October rent still owed
    assert row["total_owed"] == 12240
    assert row["received_in_month"] == 9000    # 4,000 (for Oct) + 5,000 (for Nov), both paid in Nov
    assert row["status"] == "partial"
    assert row["rent_bill_missing"] is False
    assert {b["id"] for b in row["bills"]} == {scenario["nov_rent"], scenario["nov_elec"]}
    assert len(row["payments"]) == 2


def test_register_flags_missing_rent_bill(client, admin_auth, other_tenant, other_shop, scenario):
    data = _register(client, admin_auth)
    row = next(r for r in data["rows"] if r["user_id"] == other_tenant.id)
    assert row["status"] == "no_bill"
    assert row["rent_bill_missing"] is True
    assert data["summary"]["rent_bill_missing_count"] == 1


def test_register_summary_totals(client, admin_auth, scenario):
    s = _register(client, admin_auth)["summary"]
    assert s["billed"] == 11240
    assert s["paid"] == 5000
    assert s["due"] == 6240
    assert s["received_in_month"] == 9000
    assert s["older_due"] == 6000
    assert s["total_owed"] == 12240
    assert s["partial_count"] == 1 and s["no_bill_count"] == 1


def test_register_october_shows_cash_received_zero(client, admin_auth, tenant, scenario):
    row = next(r for r in _register(client, admin_auth, month=10)["rows"] if r["user_id"] == tenant.id)
    assert row["billed"] == 10000 and row["paid"] == 4000 and row["due"] == 6000
    assert row["received_in_month"] == 0       # the 4,000 arrived in November
    assert row["status"] == "partial"


def test_register_keeps_past_tenant_who_still_owes(client, db, admin_auth, tenant, shop, scenario):
    db.query(UserShop).filter(UserShop.user_id == tenant.id).delete()
    db.commit()
    row = next(r for r in _register(client, admin_auth, month=12)["rows"] if r["user_id"] == tenant.id)
    assert row["is_current"] is False
    assert row["total_owed"] == 12240
    assert row["rent_bill_missing"] is False   # not their shop any more


def test_register_complex_filter(client, db, admin_auth, tenant, shop, scenario):
    from models.schema import Complex
    c = Complex(name="East Wing")
    db.add(c); db.commit()
    shop_row = db.get(type(shop), shop.id)
    shop_row.complex_id = c.id
    db.commit()
    rows = _register(client, admin_auth, complex_id=c.id)["rows"]
    assert {r["user_id"] for r in rows} == {tenant.id}
    assert rows[0]["complex_name"] == "East Wing"


def test_register_is_admin_only(client, tenant_auth):
    assert client.get("/api/finance/register?year=2026&month=11", headers=tenant_auth).status_code == 403


def test_register_validates_month(client, admin_auth):
    assert client.get("/api/finance/register?year=2026&month=13", headers=admin_auth).status_code == 422


# ── tenant ledger ─────────────────────────────────────────────────────────

def test_ledger_months_and_running_balance(client, admin_auth, tenant, scenario):
    res = client.get(f"/api/finance/tenant/{tenant.id}/ledger?year=2026", headers=admin_auth)
    assert res.status_code == 200, res.text
    d = res.json()
    m = {x["month"]: x for x in d["months"]}
    assert d["opening_balance"] == 0
    assert m[9]["balance_end"] == 0
    assert m[10]["billed"] == 10000 and m[10]["received"] == 0 and m[10]["balance_end"] == 10000
    assert m[11]["billed"] == 11240 and m[11]["received"] == 9000
    assert m[11]["balance_end"] == 12240        # 10,000 + 11,240 - 9,000
    assert d["closing_balance"] == 12240
    assert d["outstanding_now"] == 12240
    assert d["totals"] == {"billed": 21240, "paid": 9000, "due": 12240, "received": 9000}
    assert len(d["bills"]) == 3 and len(d["payments"]) == 2
    assert d["shops"][0]["shop_number"] == "A-101"


def test_ledger_opening_balance_from_previous_year(client, admin_auth, tenant, scenario):
    d = client.get(f"/api/finance/tenant/{tenant.id}/ledger?year=2027", headers=admin_auth).json()
    assert d["opening_balance"] == 12240
    assert d["bills"] == []


def test_ledger_unknown_tenant(client, admin_auth):
    assert client.get("/api/finance/tenant/9999/ledger?year=2026", headers=admin_auth).status_code == 404


# ── defaulters ────────────────────────────────────────────────────────────

def test_defaulters_list(client, admin_auth, tenant, scenario):
    d = client.get("/api/finance/defaulters", headers=admin_auth).json()
    assert [r["user_id"] for r in d["rows"]] == [tenant.id]
    r = d["rows"][0]
    assert r["total_owed"] == 12240
    assert r["months_unpaid"] == 2
    assert r["unpaid_bills"] == 3
    assert r["oldest_unpaid_date"].startswith("2026-10-01")
    assert r["last_payment_amount"] == 5000
    assert r["shops"][0]["shop_number"] == "A-101"


def test_defaulters_min_months_filter(client, admin_auth, scenario):
    assert client.get("/api/finance/defaulters?min_months=3", headers=admin_auth).json()["rows"] == []


def test_defaulters_exclude_fully_paid(client, admin_auth, tenant, scenario):
    # pay everything off
    _pay(client, admin_auth, scenario["oct_rent"], 6000, datetime(2026, 11, 12))
    _pay(client, admin_auth, scenario["nov_rent"], 5000, datetime(2026, 11, 12))
    _pay(client, admin_auth, scenario["nov_elec"], 1240, datetime(2026, 11, 12))
    assert client.get("/api/finance/defaulters", headers=admin_auth).json()["rows"] == []
    row = next(r for r in _register(client, admin_auth)["rows"] if r["user_id"] == tenant.id)
    assert row["status"] == "paid" and row["total_owed"] == 0
