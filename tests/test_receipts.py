"""
Receipts + advance: every money path, and every edit/delete, keeps the books right.

After EVERY test the `books_balance` fixture runs services.receipts.check_invariants():
    I1 bill.paid_amount == its payments      I2 a receipt is never over-applied
    I3 no tenant's advance is negative       I4 refunds have no payments
so a test that "passes" while quietly breaking the books still fails.

Shop A-101 rent 10,000 belongs to "Tenant One" (fixtures in conftest.py).
"""

from datetime import datetime

import pytest

from models.schema import Bill, Payment, Receipt, Shop, UserShop
from services import receipts as rs
from tests.conftest import make_jpeg


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def books_balance(db):
    yield
    db.expire_all()
    problems = rs.check_invariants(db)
    assert problems == [], "money rules broken:\n" + "\n".join(problems)


def bill(client, auth, user, shop, when, bill_type="Rent", amount=None):
    body = {"user_id": user.id, "shop_id": shop.id, "bill_type": bill_type,
            "bill_date": when.isoformat(), "due_date": when.replace(day=10).isoformat()}
    if amount is not None:
        body["amount"] = amount
    r = client.post("/api/bill", headers=auth, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def lump_sum(client, auth, user, amount, when=datetime(2026, 11, 13), shop_id=None, method="Cash"):
    """The admin's normal 'Auto allocate' flow: preview, then confirm as suggested."""
    prev = client.post("/api/payment/auto-allocate/preview", headers=auth,
                       json={"user_id": user.id, "shop_id": shop_id, "amount": amount})
    assert prev.status_code == 200, prev.text
    allocs = [{"bill_id": r["bill_id"], "amount": r["allocated"]} for r in prev.json()["rows"] if r["allocated"] > 0]
    res = client.post("/api/payment/auto-allocate/confirm", headers=auth, json={
        "user_id": user.id, "amount_received": amount, "payment_method": method,
        "allocations": allocs, "payment_date": when.isoformat()})
    assert res.status_code == 201, res.text
    return res.json()


def B(db, bill_id):
    db.expire_all()
    return db.get(Bill, bill_id)


def adv(db, user):
    db.expire_all()
    return float(rs.advance_of(db, user.id))


def receipt_of(client, auth, rid):
    r = client.get(f"/api/receipts/{rid}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def three_months_due(client, admin_auth, tenant, shop):
    """Aug, Sep, Oct and Nov 2026 rent, all unpaid (40,000 owed)."""
    return [bill(client, admin_auth, tenant, shop, datetime(2026, m, 1)) for m in (8, 9, 10, 11)]


# ══════════════════════════════════════════════════════════════════════════
# The business problem: one lump sum for several months
# ══════════════════════════════════════════════════════════════════════════

def test_lump_sum_is_one_receipt_split_month_by_month(client, db, admin_auth, tenant, three_months_due):
    res = lump_sum(client, admin_auth, tenant, 40000)
    assert res["receipt_no"].startswith("R-")
    assert res["kept_as_advance"] == 0

    r = receipt_of(client, admin_auth, res["receipt_id"])
    assert r["amount"] == 40000 and r["applied"] == 40000 and r["advance_left"] == 0
    assert [(l["period"], l["amount"]) for l in r["lines"]] == [
        ("2026-08", 10000), ("2026-09", 10000), ("2026-10", 10000), ("2026-11", 10000)]
    assert all(B(db, b).status == "paid" for b in three_months_due)
    # every slice carries the receipt's date - all cash arrived on 13 Nov
    assert {p.payment_date.date() for p in db.query(Payment).all()} == {datetime(2026, 11, 13).date()}


def test_overpayment_is_kept_as_advance_not_lost(client, db, admin_auth, tenant, three_months_due):
    res = lump_sum(client, admin_auth, tenant, 45000)
    assert res["kept_as_advance"] == 5000
    assert res["advance_balance"] == 5000
    assert adv(db, tenant) == 5000
    r = receipt_of(client, admin_auth, res["receipt_id"])
    assert r["applied"] == 40000 and r["advance_left"] == 5000


def test_preview_tells_admin_what_becomes_advance(client, admin_auth, tenant, three_months_due):
    prev = client.post("/api/payment/auto-allocate/preview", headers=admin_auth,
                       json={"user_id": tenant.id, "amount": 45000}).json()
    assert prev["unallocated_amount"] == 5000
    assert prev["current_advance"] == 0 and prev["advance_after"] == 5000


def test_advance_only_payment_when_nothing_is_owed(client, db, admin_auth, tenant, shop):
    res = client.post("/api/payment/auto-allocate/confirm", headers=admin_auth, json={
        "user_id": tenant.id, "amount_received": 20000, "payment_method": "UPI", "allocations": []})
    assert res.status_code == 201, res.text
    assert res.json()["kept_as_advance"] == 20000
    assert adv(db, tenant) == 20000


def test_next_bill_is_paid_from_advance_automatically(client, db, admin_auth, tenant, shop, three_months_due):
    lump_sum(client, admin_auth, tenant, 45000)
    dec = bill(client, admin_auth, tenant, shop, datetime(2026, 12, 1))
    b = B(db, dec)
    assert float(b.paid_amount) == 5000 and float(b.pending_amount) == 5000 and b.status == "partial"
    assert adv(db, tenant) == 0
    # the advance slice is dated when the cash came in, not when it was used
    p = db.query(Payment).filter(Payment.bill_id == dec).one()
    assert p.payment_date.date() == datetime(2026, 11, 13).date()


def test_advance_pays_any_of_the_tenants_shops(client, db, admin_auth, tenant, shop):
    s2 = Shop(shop_number="A-102", status="occupied", shop_rent=6000, shop_deposit=0)
    db.add(s2); db.commit(); db.refresh(s2)
    db.add(UserShop(user_id=tenant.id, shop_id=s2.id)); db.commit()
    client.post("/api/payment/auto-allocate/confirm", headers=admin_auth, json={
        "user_id": tenant.id, "amount_received": 8000, "payment_method": "Cash", "allocations": []})
    other = bill(client, admin_auth, tenant, s2, datetime(2026, 12, 1))
    assert B(db, other).status == "paid"
    assert adv(db, tenant) == 2000


def test_manual_single_bill_payment_over_the_due_keeps_the_rest(client, db, admin_auth, tenant, shop):
    b1 = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    b2 = bill(client, admin_auth, tenant, shop, datetime(2026, 11, 1))
    r = client.post("/api/payment", headers=admin_auth, json={
        "bill_id": b1, "amount": 15000, "payment_method": "Cash"})
    assert r.status_code == 201, r.text
    assert r.json()["amount"] == 10000            # the chosen bill gets what it owes
    assert B(db, b1).status == "paid"
    assert float(B(db, b2).paid_amount) == 5000   # the rest pays the next bill, oldest first
    assert adv(db, tenant) == 0


# ══════════════════════════════════════════════════════════════════════════
# Editing / deleting BILLS
# ══════════════════════════════════════════════════════════════════════════

def test_delete_paid_bill_money_goes_back_to_advance(client, db, admin_auth, tenant, shop):
    paid = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    lump_sum(client, admin_auth, tenant, 10000)
    assert client.delete(f"/api/bill/{paid}", headers=admin_auth).status_code == 200
    assert adv(db, tenant) == 10000                # not lost
    assert db.query(Receipt).count() == 1


def test_delete_paid_bill_money_moves_to_other_unpaid_bill(client, db, admin_auth, tenant, shop):
    wrong = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    lump_sum(client, admin_auth, tenant, 10000)
    other = bill(client, admin_auth, tenant, shop, datetime(2026, 11, 1))
    assert B(db, other).status == "pending"
    client.delete(f"/api/bill/{wrong}", headers=admin_auth)
    assert B(db, other).status == "paid"
    assert adv(db, tenant) == 0


def test_lower_bill_amount_below_paid_releases_to_advance(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    lump_sum(client, admin_auth, tenant, 10000)
    r = client.put(f"/api/bill/{b}", headers=admin_auth, json={"amount": 7000})
    assert r.status_code == 200, r.text          # used to be a 400
    assert B(db, b).status == "paid" and float(B(db, b).paid_amount) == 7000
    assert adv(db, tenant) == 3000


def test_raise_bill_amount_uses_advance(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    lump_sum(client, admin_auth, tenant, 12000)
    assert adv(db, tenant) == 2000
    client.put(f"/api/bill/{b}", headers=admin_auth, json={"amount": 11000})
    assert B(db, b).status == "paid" and adv(db, tenant) == 1000


def test_edit_bill_non_money_fields_changes_nothing(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    lump_sum(client, admin_auth, tenant, 4000)
    client.put(f"/api/bill/{b}", headers=admin_auth, json={"description": "fixed typo",
                                                            "due_date": "2026-10-20T00:00:00"})
    assert float(B(db, b).paid_amount) == 4000 and adv(db, tenant) == 0


def test_delete_bill_with_late_fee_keeps_both_payments(client, db, admin_auth, tenant, shop):
    parent = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    fee = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Penalty", amount=500, paid_amount=0,
               pending_amount=500, status="pending", parent_bill_id=parent,
               bill_date=datetime(2026, 10, 20), due_date=datetime(2026, 10, 25))
    db.add(fee); db.commit()
    lump_sum(client, admin_auth, tenant, 10500)
    client.delete(f"/api/bill/{parent}", headers=admin_auth)
    assert db.query(Bill).count() == 0
    assert adv(db, tenant) == 10500


# ══════════════════════════════════════════════════════════════════════════
# Editing / deleting RECEIPTS
# ══════════════════════════════════════════════════════════════════════════

def test_reduce_receipt_takes_back_from_newest_bill(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 40000)["receipt_id"]
    r = client.put(f"/api/receipts/{rid}", headers=admin_auth, json={"amount": 35000})
    assert r.status_code == 200, r.text
    aug, sep, oct_, nov = three_months_due
    assert B(db, aug).status == "paid" and B(db, oct_).status == "paid"
    assert float(B(db, nov).pending_amount) == 5000      # newest gave it back


def test_reduce_receipt_uses_its_unused_advance_first(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 45000)["receipt_id"]
    client.put(f"/api/receipts/{rid}", headers=admin_auth, json={"amount": 42000})
    assert all(B(db, b).status == "paid" for b in three_months_due)
    assert adv(db, tenant) == 2000


def test_increase_receipt_pays_more_then_advance(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 30000)["receipt_id"]
    client.put(f"/api/receipts/{rid}", headers=admin_auth, json={"amount": 41000})
    assert all(B(db, b).status == "paid" for b in three_months_due)
    assert adv(db, tenant) == 1000


def test_edit_receipt_date_moves_every_slice(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 40000)["receipt_id"]
    client.put(f"/api/receipts/{rid}", headers=admin_auth,
               json={"received_date": "2026-12-02T10:00:00", "payment_method": "UPI"})
    pays = db.query(Payment).all()
    assert {p.payment_date.date() for p in pays} == {datetime(2026, 12, 2).date()}
    assert {p.payment_method for p in pays} == {"UPI"}
    nov = client.get("/api/finance/register?year=2026&month=11", headers=admin_auth).json()
    dec = client.get("/api/finance/register?year=2026&month=12", headers=admin_auth).json()
    assert nov["summary"]["received_in_month"] == 0
    assert dec["summary"]["received_in_month"] == 40000


def test_delete_receipt_reopens_bills(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 40000)["receipt_id"]
    assert client.delete(f"/api/receipts/{rid}", headers=admin_auth).status_code == 200
    assert all(B(db, b).status == "pending" for b in three_months_due)
    assert db.query(Payment).count() == 0 and adv(db, tenant) == 0


def test_delete_one_receipt_other_advance_steps_in(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    first = lump_sum(client, admin_auth, tenant, 10000)["receipt_id"]
    client.post("/api/payment/auto-allocate/confirm", headers=admin_auth, json={
        "user_id": tenant.id, "amount_received": 10000, "payment_method": "Cash", "allocations": []})
    assert adv(db, tenant) == 10000
    client.delete(f"/api/receipts/{first}", headers=admin_auth)
    assert B(db, b).status == "paid"            # the second receipt's advance covers it
    assert adv(db, tenant) == 0


# ══════════════════════════════════════════════════════════════════════════
# Editing / deleting a single PAYMENT LINE (old screens still use these)
# ══════════════════════════════════════════════════════════════════════════

def test_edit_payment_line_amount_changes_money_received(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 20000)["receipt_id"]
    aug = three_months_due[0]
    pay = db.query(Payment).filter(Payment.bill_id == aug).one()
    r = client.put(f"/api/payment/{pay.id}", headers=admin_auth, json={"amount": 6000})
    assert r.status_code == 200, r.text
    assert receipt_of(client, admin_auth, rid)["amount"] == 16000
    assert float(B(db, aug).pending_amount) == 4000


def test_edit_payment_line_above_bill_keeps_extra_as_advance(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    rid = lump_sum(client, admin_auth, tenant, 10000)["receipt_id"]
    pay = db.query(Payment).filter(Payment.bill_id == b).one()
    client.put(f"/api/payment/{pay.id}", headers=admin_auth, json={"amount": 12000})
    assert float(B(db, b).paid_amount) == 10000            # never overpaid
    assert receipt_of(client, admin_auth, rid)["amount"] == 12000
    assert adv(db, tenant) == 2000


def test_edit_payment_line_date_applies_to_whole_receipt(client, db, admin_auth, tenant, three_months_due):
    lump_sum(client, admin_auth, tenant, 20000)
    pay = db.query(Payment).first()
    client.put(f"/api/payment/{pay.id}", headers=admin_auth, json={"payment_date": "2026-11-01T09:00:00"})
    db.expire_all()
    assert {p.payment_date.date() for p in db.query(Payment).all()} == {datetime(2026, 11, 1).date()}


def test_delete_payment_line_means_money_not_received(client, db, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 20000)["receipt_id"]
    aug = three_months_due[0]
    pay = db.query(Payment).filter(Payment.bill_id == aug).one()
    assert client.delete(f"/api/payment/{pay.id}", headers=admin_auth).status_code == 200
    assert receipt_of(client, admin_auth, rid)["amount"] == 10000
    assert B(db, aug).status == "pending" and adv(db, tenant) == 0


def test_delete_last_payment_line_removes_empty_receipt(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    client.post("/api/payment", headers=admin_auth, json={"bill_id": b, "amount": 3000, "payment_method": "Cash"})
    pay = db.query(Payment).one()
    client.delete(f"/api/payment/{pay.id}", headers=admin_auth)
    assert db.query(Receipt).count() == 0


# ══════════════════════════════════════════════════════════════════════════
# REFUNDS
# ══════════════════════════════════════════════════════════════════════════

def test_refund_reduces_advance(client, db, admin_auth, tenant, shop):
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 8000, "payment_method": "Cash"})
    r = client.post("/api/receipts/refund", headers=admin_auth,
                    json={"user_id": tenant.id, "amount": 3000, "remarks": "tenant left"})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "refund" and r.json()["tenant_advance"] == 5000


def test_refund_cannot_exceed_advance(client, admin_auth, tenant, shop):
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 2000, "payment_method": "Cash"})
    r = client.post("/api/receipts/refund", headers=admin_auth, json={"user_id": tenant.id, "amount": 2500})
    assert r.status_code == 400 and "advance" in r.json()["detail"].lower()


def test_cannot_delete_receipt_whose_money_was_refunded(client, db, admin_auth, tenant, shop):
    rid = client.post("/api/receipts", headers=admin_auth,
                      json={"user_id": tenant.id, "amount": 5000, "payment_method": "Cash"}).json()["id"]
    client.post("/api/receipts/refund", headers=admin_auth, json={"user_id": tenant.id, "amount": 5000})
    r = client.delete(f"/api/receipts/{rid}", headers=admin_auth)
    assert r.status_code == 400 and "refund" in r.json()["detail"].lower()
    r = client.put(f"/api/receipts/{rid}", headers=admin_auth, json={"amount": 1000})
    assert r.status_code == 400
    assert db.query(Receipt).count() == 2           # nothing changed


def test_delete_refund_gives_advance_back_and_pays_bills(client, db, admin_auth, tenant, shop):
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 10000, "payment_method": "Cash"})
    ref = client.post("/api/receipts/refund", headers=admin_auth,
                      json={"user_id": tenant.id, "amount": 10000}).json()["id"]
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 12, 1))
    assert B(db, b).status == "pending"
    client.delete(f"/api/receipts/{ref}", headers=admin_auth)
    assert B(db, b).status == "paid"


# ══════════════════════════════════════════════════════════════════════════
# Bills created OUTSIDE the API (cron scheduler raw SQL) and electricity bills
# ══════════════════════════════════════════════════════════════════════════

def _cron_style_bill(db, tenant, shop, when, amount=10000):
    b = Bill(user_id=tenant.id, shop_id=shop.id, bill_type="Rent", amount=amount, paid_amount=0,
             pending_amount=amount, status="pending", bill_date=when, due_date=when.replace(day=10),
             rent_period=f"RENT-{when:%Y-%m}")
    db.add(b); db.commit(); db.refresh(b)
    return b.id


def test_cron_bill_settled_when_tenant_opens_portal(client, db, admin_auth, tenant_auth, tenant, shop):
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 10000, "payment_method": "Cash"})
    bid = _cron_style_bill(db, tenant, shop, datetime(2026, 12, 1))
    home = client.get("/api/tenant/home", headers=tenant_auth).json()
    assert next(b for b in home["bills"] if b["id"] == bid)["status"] == "paid"
    assert home["advance"] == 0


def test_cron_bill_settled_by_finance_screen_and_cli(client, db, admin_auth, tenant, shop):
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 10000, "payment_method": "Cash"})
    bid = _cron_style_bill(db, tenant, shop, datetime(2026, 12, 1))
    client.get("/api/finance/register?year=2026&month=12", headers=admin_auth)
    assert B(db, bid).status == "paid"
    bid2 = _cron_style_bill(db, tenant, shop, datetime(2027, 1, 1))
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 10000, "payment_method": "Cash"})
    assert B(db, bid2).status == "paid"


def test_electricity_bill_from_meter_reading_uses_advance(client, db, admin_auth, tenant_auth, tenant,
                                                         meter, tariff, photo_dir):
    client.post("/api/receipts", headers=admin_auth,
                json={"user_id": tenant.id, "amount": 5000, "payment_method": "Cash"})
    sub = client.post("/api/tenant/meter-readings", headers=tenant_auth,
                      data={"meter_id": str(meter.id), "customer_reading": "12550"},
                      files={"photo": ("m.jpg", make_jpeg(), "image/jpeg")})
    assert sub.status_code == 201, sub.text
    rid = sub.json()["reading"]["id"]
    ap = client.post(f"/api/meter-readings/{rid}/approve", headers=admin_auth,
                     json={"admin_verified_reading": 12550})
    assert ap.status_code == 200, ap.text
    elec = db.query(Bill).filter(Bill.bill_type == "Electricity").one()
    assert elec.status == "paid"                    # 100 units x 9.50 = 950
    assert adv(db, tenant) == 5000 - 950


# ══════════════════════════════════════════════════════════════════════════
# Screens: register, ledger, tenant portal, access
# ══════════════════════════════════════════════════════════════════════════

def test_register_and_ledger_show_advance(client, admin_auth, tenant, three_months_due):
    lump_sum(client, admin_auth, tenant, 45000)
    reg = client.get("/api/finance/register?year=2026&month=11", headers=admin_auth).json()
    assert reg["summary"]["received_in_month"] == 45000        # advance part included
    assert reg["summary"]["advance_held"] == 5000
    row = next(r for r in reg["rows"] if r["user_id"] == tenant.id)
    assert row["tenant_advance"] == 5000
    led = client.get(f"/api/finance/tenant/{tenant.id}/ledger?year=2026", headers=admin_auth).json()
    nov = next(m for m in led["months"] if m["month"] == 11)
    assert nov["received"] == 45000 and nov["balance_end"] == -5000   # in credit
    assert led["advance_now"] == 5000 and len(led["receipts"]) == 1
    assert led["receipts"][0]["advance_left"] == 5000


def test_tenant_sees_own_receipts_only(client, admin_auth, tenant, tenant_auth, other_tenant_auth, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 45000)["receipt_id"]
    mine = client.get("/api/tenant/receipts", headers=tenant_auth).json()
    assert mine["advance"] == 5000
    assert mine["receipts"][0]["lines"][0]["shop_number"] == "A-101"
    home = client.get("/api/tenant/home", headers=tenant_auth).json()
    assert home["advance"] == 5000 and home["receipts"][0]["amount"] == 45000
    assert home["receipts"][0]["advance_left"] == 5000
    assert client.get(f"/api/tenant/receipts/{rid}", headers=tenant_auth).status_code == 200
    assert client.get(f"/api/tenant/receipts/{rid}", headers=other_tenant_auth).status_code == 404
    assert client.get("/api/tenant/receipts", headers=other_tenant_auth).json()["receipts"] == []


def test_tenant_payments_grouped_by_receipt(client, admin_auth, tenant_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 40000)["receipt_id"]
    pays = client.get("/api/tenant/payments", headers=tenant_auth).json()
    assert len(pays) == 4 and {p["payment_group"] for p in pays} == {rid}


def test_receipt_admin_endpoints_are_admin_only(client, tenant_auth, tenant):
    assert client.get("/api/receipts", headers=tenant_auth).status_code == 403
    assert client.post("/api/receipts/refund", headers=tenant_auth,
                       json={"user_id": tenant.id, "amount": 1}).status_code == 403


def test_receipts_list_search_and_check(client, admin_auth, tenant, three_months_due):
    lump_sum(client, admin_auth, tenant, 40000)
    lst = client.get("/api/receipts?search=Tenant One", headers=admin_auth).json()
    assert lst["total"] == 1 and lst["data"][0]["tenant_name"] == "Tenant One"
    chk = client.get("/api/receipts/check", headers=admin_auth).json()
    assert chk["ok"] is True and chk["payments_without_receipt"] == 0


# ══════════════════════════════════════════════════════════════════════════
# Old data (payments from before receipts)
# ══════════════════════════════════════════════════════════════════════════

def _legacy_payment(db, bill_id, amount, when, created):
    from helpers.domain import _reconcile_bill
    p = Payment(bill_id=bill_id, amount=amount, payment_method="Cash", payment_date=when,
                remarks=None, created_at=created)
    db.add(p); db.flush()
    b = db.get(Bill, bill_id); db.refresh(b); _reconcile_bill(b)
    db.commit()
    return p.id


def test_backfill_groups_one_lump_sum_into_one_receipt(client, db, admin_auth, tenant, three_months_due):
    when = datetime(2026, 11, 13, 10, 0)
    for i, b in enumerate(three_months_due[:3]):
        _legacy_payment(db, b, 10000, when, datetime(2026, 11, 13, 10, 0, i))
    _legacy_payment(db, three_months_due[3], 2000, datetime(2026, 11, 20), datetime(2026, 11, 20, 9))

    plan = rs.run_backfill(db, dry_run=True)
    assert plan["receipts"] == 2 and plan["payments"] == 4
    assert db.query(Receipt).count() == 0          # dry run wrote nothing
    db.rollback()

    rs.run_backfill(db, dry_run=False); db.commit()
    amounts = sorted(float(r.amount) for r in db.query(Receipt).all())
    assert amounts == [2000.0, 30000.0]
    assert db.query(Payment).filter(Payment.receipt_id.is_(None)).count() == 0


def test_deleting_bill_with_legacy_payment_keeps_the_money(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    _legacy_payment(db, b, 10000, datetime(2026, 10, 5), datetime(2026, 10, 5))
    client.delete(f"/api/bill/{b}", headers=admin_auth)
    assert adv(db, tenant) == 10000                 # adopted into a receipt, not lost


def test_legacy_payment_still_editable(client, db, admin_auth, tenant, shop):
    b = bill(client, admin_auth, tenant, shop, datetime(2026, 10, 1))
    pid = _legacy_payment(db, b, 4000, datetime(2026, 10, 5), datetime(2026, 10, 5))
    assert client.put(f"/api/payment/{pid}", headers=admin_auth, json={"amount": 5000}).status_code == 200
    assert float(B(db, b).paid_amount) == 5000
    assert client.delete(f"/api/payment/{pid}", headers=admin_auth).status_code == 200
    assert float(B(db, b).paid_amount) == 0


def test_receipt_shows_advance_left_after_refund(client, admin_auth, tenant, three_months_due):
    rid = lump_sum(client, admin_auth, tenant, 45000)["receipt_id"]
    client.post("/api/receipts/refund", headers=admin_auth, json={"user_id": tenant.id, "amount": 1000})
    one = receipt_of(client, admin_auth, rid)
    assert one["advance_left"] == 4000 and one["applied"] == 40000
    listed = client.get(f"/api/receipts?user_id={tenant.id}&kind=receipt", headers=admin_auth).json()["data"][0]
    assert listed["advance_left"] == 4000
    led = client.get(f"/api/finance/tenant/{tenant.id}/ledger?year=2026", headers=admin_auth).json()
    rec = next(r for r in led["receipts"] if r["kind"] == "receipt")
    assert rec["advance_left"] == 4000 and led["advance_now"] == 4000
