"""
routers/finance.py - the admin Finance section (rebuilt 2026-11).

Three read-only views, each answering one question an admin actually asks:

    GET /api/finance/register        "For this month: who was billed what, who
                                      has paid, who still owes?"
    GET /api/finance/tenant/{id}/ledger
                                     "Month by month, what has this tenant been
                                      billed, paid, and what do they owe?"
    GET /api/finance/defaulters      "Who owes money, how much, since when?"

Creating, editing and deleting bills/payments still goes through the existing
/api/bill and /api/payment endpoints - this module only reads.

Definitions used everywhere below (and shown to the admin as-is):
    month of a bill  = its bill_date month (rent_period follows bill_date too)
    billed           = bill_payable(bill)  (amount + any penalty)
    paid (against)   = bill.paid_amount    payments applied to that bill, made
                                           at any time
    due              = bill.pending_amount what is still unpaid on it today
    cash received    = payments whose payment_date falls in the month, for
                       whichever bill
    older due        = pending_amount, today, of bills dated before the month

All date filtering is done with Python date ranges (not MONTH()/YEAR() SQL
functions) so the same code runs on MySQL in production and SQLite in tests.

Step 27 of the router/service split (new module).
"""

from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.config import APP_TIMEZONE
from core.database import get_db
from core.security import require_admin
from helpers.domain import _decimal_to_float, bill_payable, is_late_fee_bill
from models.schema import Bill, Complex, Payment, Shop, User, UserShop
from services import settings as settings_service

router = APIRouter(tags=["Finance"])


# ── small helpers ─────────────────────────────────────────────────────────

def _month_range(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def _today() -> date:
    return datetime.now(ZoneInfo(APP_TIMEZONE)).date()


def _r(x: float) -> float:
    return round(float(x or 0), 2)


def _category(bill: Bill, electricity_label: str) -> str:
    """rent / electricity / late_fee / other - the register's columns."""
    if is_late_fee_bill(bill):
        return "late_fee"
    t = (bill.bill_type or "").strip().lower()
    if t == "rent":
        return "rent"
    if t in {"electricity", (electricity_label or "").strip().lower()}:
        return "electricity"
    if t in {"penalty", "late fee", "late_fee"}:
        return "late_fee"
    return "other"


def _is_overdue(bill: Bill, today: date) -> bool:
    return (
        _decimal_to_float(bill.pending_amount) > 0
        and bill.due_date is not None
        and bill.due_date.date() < today
    )


def _bill_dict(bill: Bill, electricity_label: str, today: date) -> dict:
    return {
        "id": bill.id,
        "user_id": bill.user_id,
        "shop_id": bill.shop_id,
        "bill_type": bill.bill_type,
        "category": _category(bill, electricity_label),
        "description": bill.description,
        "amount": _r(_decimal_to_float(bill.amount)),
        "payable": _r(bill_payable(bill)),
        "paid": _r(_decimal_to_float(bill.paid_amount)),
        "pending": _r(_decimal_to_float(bill.pending_amount)),
        "bill_date": bill.bill_date.isoformat() if bill.bill_date else None,
        "due_date": bill.due_date.isoformat() if bill.due_date else None,
        "status": bill.status,
        "overdue": _is_overdue(bill, today),
        "parent_bill_id": bill.parent_bill_id,
    }


def _payment_dict(p: Payment, bill: Optional[Bill]) -> dict:
    return {
        "id": p.id,
        "bill_id": p.bill_id,
        "shop_id": bill.shop_id if bill else None,
        "bill_type": bill.bill_type if bill else None,
        "bill_date": bill.bill_date.isoformat() if bill and bill.bill_date else None,
        "amount": _r(_decimal_to_float(p.amount)),
        "payment_method": p.payment_method,
        "payment_date": p.payment_date.isoformat() if p.payment_date else None,
        "remarks": p.remarks,
        "online": bool(p.razorpay_payment_id),
    }


def _status(billed: float, paid: float, due: float) -> str:
    if billed <= 0:
        return "no_bill"
    if due <= 0.005:
        return "paid"
    if paid > 0:
        return "partial"
    return "unpaid"


def _lookup_maps(db: Session, user_ids, shop_ids):
    users = {
        u.id: u for u in db.query(User).filter(User.id.in_(list(user_ids))).all()
    } if user_ids else {}
    shops = {
        s.id: s for s in db.query(Shop).filter(Shop.id.in_(list(shop_ids))).all()
    } if shop_ids else {}
    complexes = {c.id: c.name for c in db.query(Complex).all()}
    return users, shops, complexes


# ══════════════════════════════════════════════════════════════════════════
# 1. MONTH REGISTER
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/finance/register")
def finance_register(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    complex_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    One row per tenant + shop for the chosen month.

    Rows come from: every current assignment, plus anyone billed in the month,
    plus anyone (current or past tenant) who still owes on an older bill - so a
    tenant who left owing money never silently drops off the screen.
    """
    start, end = _month_range(year, month)
    today = _today()
    cfg = settings_service.get_all(db)
    elec_label = str(cfg.get("meter.bill_type_label") or "Electricity")

    def in_complex(q, shop_col):
        if complex_id is None:
            return q
        return q.join(Shop, Shop.id == shop_col).filter(Shop.complex_id == complex_id)

    month_bills = in_complex(
        db.query(Bill).filter(Bill.bill_date >= start, Bill.bill_date < end), Bill.shop_id
    ).all()
    older_unpaid = in_complex(
        db.query(Bill).filter(Bill.bill_date < start, Bill.pending_amount > 0), Bill.shop_id
    ).all()
    month_payments = in_complex(
        db.query(Payment, Bill).join(Bill, Bill.id == Payment.bill_id)
        .filter(Payment.payment_date >= start, Payment.payment_date < end),
        Bill.shop_id,
    ).all()
    assignments = in_complex(db.query(UserShop), UserShop.shop_id).all()

    keys = set()
    current = set()
    for a in assignments:
        keys.add((a.user_id, a.shop_id))
        current.add((a.user_id, a.shop_id))
    for b in month_bills + older_unpaid:
        keys.add((b.user_id, b.shop_id))
    for p, b in month_payments:
        keys.add((b.user_id, b.shop_id))

    users, shops, complexes = _lookup_maps(db, {k[0] for k in keys}, {k[1] for k in keys})
    assign_by_key = {(a.user_id, a.shop_id): a for a in assignments}

    rows: Dict[tuple, dict] = {}
    for key in keys:
        uid, sid = key
        u, s = users.get(uid), shops.get(sid)
        if not u or u.role != "tenant":
            continue
        a = assign_by_key.get(key)
        rows[key] = {
            "user_id": uid,
            "tenant_name": u.name,
            "mobile": u.mobile,
            "shop_id": sid,
            "shop_number": s.shop_number if s else None,
            "complex_id": s.complex_id if s else None,
            "complex_name": complexes.get(s.complex_id) if s else None,
            "monthly_rent": _r(_decimal_to_float(s.shop_rent)) if s else 0.0,
            "is_current": key in current,
            "agreement_end_date": a.agreement_end_date.isoformat() if a and a.agreement_end_date else None,
            "rent": 0.0, "electricity": 0.0, "late_fee": 0.0, "other": 0.0,
            "billed": 0.0, "paid": 0.0, "due": 0.0,
            "older_due": 0.0, "older_unpaid_bills": 0,
            "received_in_month": 0.0,
            "overdue": False,
            "bills": [],
            "payments": [],
        }

    for b in month_bills:
        row = rows.get((b.user_id, b.shop_id))
        if not row:
            continue
        d = _bill_dict(b, elec_label, today)
        row[d["category"]] += d["payable"]
        row["billed"] += d["payable"]
        row["paid"] += d["paid"]
        row["due"] += d["pending"]
        row["overdue"] = row["overdue"] or d["overdue"]
        row["bills"].append(d)

    for b in older_unpaid:
        row = rows.get((b.user_id, b.shop_id))
        if not row:
            continue
        row["older_due"] += _decimal_to_float(b.pending_amount)
        row["older_unpaid_bills"] += 1
        row["overdue"] = row["overdue"] or _is_overdue(b, today)

    for p, b in month_payments:
        row = rows.get((b.user_id, b.shop_id))
        if not row:
            continue
        row["received_in_month"] += _decimal_to_float(p.amount)
        row["payments"].append(_payment_dict(p, b))

    out = []
    for row in rows.values():
        for k in ("rent", "electricity", "late_fee", "other", "billed", "paid", "due",
                  "older_due", "received_in_month"):
            row[k] = _r(row[k])
        row["total_owed"] = _r(row["due"] + row["older_due"])
        row["status"] = _status(row["billed"], row["paid"], row["due"])
        row["rent_bill_missing"] = bool(
            row["is_current"] and row["monthly_rent"] > 0
            and not any(b["category"] == "rent" for b in row["bills"])
        )
        row["bills"].sort(key=lambda x: (x["bill_date"] or "", x["id"]))
        row["payments"].sort(key=lambda x: (x["payment_date"] or "", x["id"]))
        # A past tenant with nothing in this month and nothing owed is noise.
        if not row["is_current"] and row["billed"] == 0 and row["total_owed"] == 0 \
                and row["received_in_month"] == 0:
            continue
        out.append(row)

    out.sort(key=lambda r: ((r["complex_name"] or "").lower(), (r["shop_number"] or "").lower(),
                            r["tenant_name"].lower()))

    def total(k):
        return _r(sum(r[k] for r in out))

    counts = defaultdict(int)
    for r in out:
        counts[r["status"]] += 1
    summary = {
        "billed": total("billed"),
        "paid": total("paid"),
        "due": total("due"),
        "received_in_month": total("received_in_month"),
        "older_due": total("older_due"),
        "total_owed": total("total_owed"),
        "rent": total("rent"), "electricity": total("electricity"),
        "late_fee": total("late_fee"), "other": total("other"),
        "rows": len(out),
        "paid_count": counts["paid"],
        "partial_count": counts["partial"],
        "unpaid_count": counts["unpaid"],
        "no_bill_count": counts["no_bill"],
        "rent_bill_missing_count": sum(1 for r in out if r["rent_bill_missing"]),
        "collection_percent": _r(100 * total("paid") / total("billed")) if total("billed") else 0.0,
    }
    return {
        "year": year, "month": month, "complex_id": complex_id,
        "summary": summary, "rows": out,
    }


# ══════════════════════════════════════════════════════════════════════════
# 2. TENANT LEDGER
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/finance/tenant/{user_id}/ledger")
def finance_tenant_ledger(
    user_id: int,
    year: int = Query(..., ge=2000, le=2100),
    shop_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Twelve months for one tenant (optionally one shop), with a running balance.

    balance_end = everything billed up to the end of the month minus every
    payment made up to the end of the month - "what did they owe on that
    day". (Penalties are taken at today's value, which is the only value
    stored.)
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="Tenant not found")

    today = _today()
    cfg = settings_service.get_all(db)
    elec_label = str(cfg.get("meter.bill_type_label") or "Electricity")

    bq = db.query(Bill).filter(Bill.user_id == user_id)
    if shop_id is not None:
        bq = bq.filter(Bill.shop_id == shop_id)
    all_bills = bq.all()
    bill_by_id = {b.id: b for b in all_bills}
    all_payments = (
        db.query(Payment).filter(Payment.bill_id.in_(list(bill_by_id))).all()
        if bill_by_id else []
    )

    year_start, _e = _month_range(year, 1)
    opening = _r(
        sum(bill_payable(b) for b in all_bills if b.bill_date < year_start)
        - sum(_decimal_to_float(p.amount) for p in all_payments if p.payment_date < year_start)
    )

    months = []
    for m in range(1, 13):
        start, end = _month_range(year, m)
        mb = [b for b in all_bills if start <= b.bill_date < end]
        mp = [p for p in all_payments if start <= p.payment_date < end]
        billed = sum(bill_payable(b) for b in mb)
        paid = sum(_decimal_to_float(b.paid_amount) for b in mb)
        due = sum(_decimal_to_float(b.pending_amount) for b in mb)
        received = sum(_decimal_to_float(p.amount) for p in mp)
        balance_end = (
            sum(bill_payable(b) for b in all_bills if b.bill_date < end)
            - sum(_decimal_to_float(p.amount) for p in all_payments if p.payment_date < end)
        )
        months.append({
            "month": m,
            "billed": _r(billed), "paid": _r(paid), "due": _r(due),
            "received": _r(received),
            "balance_end": _r(balance_end),
            "status": _status(billed, paid, due),
            "bill_count": len(mb), "payment_count": len(mp),
            "is_future": start.date() > today,
        })

    year_bills = sorted(
        (b for b in all_bills if b.bill_date.year == year),
        key=lambda b: (b.bill_date, b.id),
    )
    year_payments = sorted(
        (p for p in all_payments if p.payment_date.year == year),
        key=lambda p: (p.payment_date, p.id),
    )

    shop_ids = {b.shop_id for b in all_bills}
    assignments = db.query(UserShop).filter(UserShop.user_id == user_id).all()
    shop_ids |= {a.shop_id for a in assignments}
    _u, shops, complexes = _lookup_maps(db, set(), shop_ids)
    current_ids = {a.shop_id for a in assignments}

    years_with_data = sorted({b.bill_date.year for b in all_bills}
                             | {p.payment_date.year for p in all_payments}
                             | {today.year})

    return {
        "tenant": {"id": user.id, "name": user.name, "mobile": user.mobile, "email": user.email},
        "year": year,
        "shop_id": shop_id,
        "shops": [
            {
                "id": sid,
                "shop_number": shops[sid].shop_number if sid in shops else None,
                "complex_name": complexes.get(shops[sid].complex_id) if sid in shops else None,
                "monthly_rent": _r(_decimal_to_float(shops[sid].shop_rent)) if sid in shops else 0.0,
                "is_current": sid in current_ids,
            }
            for sid in sorted(shop_ids)
        ],
        "years": years_with_data,
        "opening_balance": opening,
        "closing_balance": months[-1]["balance_end"],
        "totals": {
            "billed": _r(sum(x["billed"] for x in months)),
            "paid": _r(sum(x["paid"] for x in months)),
            "due": _r(sum(x["due"] for x in months)),
            "received": _r(sum(x["received"] for x in months)),
        },
        "outstanding_now": _r(sum(_decimal_to_float(b.pending_amount) for b in all_bills)),
        "months": months,
        "bills": [_bill_dict(b, elec_label, today) for b in year_bills],
        "payments": [_payment_dict(p, bill_by_id.get(p.bill_id)) for p in year_payments],
    }


# ══════════════════════════════════════════════════════════════════════════
# 3. DEFAULTERS
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/finance/defaulters")
def finance_defaulters(
    min_months: int = Query(1, ge=1, le=120, description="Only tenants with at least this many months unpaid"),
    complex_id: Optional[int] = None,
    overdue_only: bool = Query(False, description="Count only bills past their due date"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Every tenant who owes money, largest amount first."""
    today = _today()
    q = db.query(Bill).filter(Bill.pending_amount > 0)
    if complex_id is not None:
        q = q.join(Shop, Shop.id == Bill.shop_id).filter(Shop.complex_id == complex_id)
    unpaid = q.all()

    by_user: Dict[int, List[Bill]] = defaultdict(list)
    for b in unpaid:
        if overdue_only and not _is_overdue(b, today):
            continue
        by_user[b.user_id].append(b)

    user_ids = set(by_user)
    shop_ids = {b.shop_id for bs in by_user.values() for b in bs}
    users, shops, complexes = _lookup_maps(db, user_ids, shop_ids)
    current = {(a.user_id, a.shop_id) for a in
               db.query(UserShop).filter(UserShop.user_id.in_(list(user_ids))).all()} if user_ids else set()

    last_pay: Dict[int, Tuple[datetime, float]] = {}
    if user_ids:
        for p, b in (db.query(Payment, Bill).join(Bill, Bill.id == Payment.bill_id)
                     .filter(Bill.user_id.in_(list(user_ids))).all()):
            prev = last_pay.get(b.user_id)
            if prev is None or p.payment_date > prev[0]:
                last_pay[b.user_id] = (p.payment_date, _decimal_to_float(p.amount))

    out = []
    for uid, bills in by_user.items():
        u = users.get(uid)
        if not u or u.role != "tenant":
            continue
        months_unpaid = len({(b.bill_date.year, b.bill_date.month) for b in bills})
        if months_unpaid < min_months:
            continue
        oldest = min(bills, key=lambda b: b.bill_date)
        lp = last_pay.get(uid)
        tenant_shops = sorted({b.shop_id for b in bills})
        out.append({
            "user_id": uid,
            "tenant_name": u.name,
            "mobile": u.mobile,
            "shops": [
                {
                    "id": sid,
                    "shop_number": shops[sid].shop_number if sid in shops else None,
                    "complex_name": complexes.get(shops[sid].complex_id) if sid in shops else None,
                    "is_current": (uid, sid) in current,
                }
                for sid in tenant_shops
            ],
            "is_current_tenant": any((uid, sid) in current for sid in tenant_shops),
            "total_owed": _r(sum(_decimal_to_float(b.pending_amount) for b in bills)),
            "overdue_amount": _r(sum(_decimal_to_float(b.pending_amount) for b in bills if _is_overdue(b, today))),
            "unpaid_bills": len(bills),
            "months_unpaid": months_unpaid,
            "oldest_unpaid_date": oldest.bill_date.isoformat(),
            "oldest_unpaid_days": (today - oldest.bill_date.date()).days,
            "last_payment_date": lp[0].isoformat() if lp else None,
            "last_payment_amount": _r(lp[1]) if lp else None,
        })

    out.sort(key=lambda r: (-r["total_owed"], r["tenant_name"].lower()))
    return {
        "min_months": min_months,
        "complex_id": complex_id,
        "overdue_only": overdue_only,
        "summary": {
            "tenants": len(out),
            "total_owed": _r(sum(r["total_owed"] for r in out)),
            "overdue_amount": _r(sum(r["overdue_amount"] for r in out)),
        },
        "rows": out,
    }
