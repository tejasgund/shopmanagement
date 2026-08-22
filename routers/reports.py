"""
routers/reports.py - GET /api/reports/* + GET /api/finance/overview (Admin only).

Extracted verbatim from app.py (step 5 of the router/service split, after
schemas.py, auth_service.py/audit_service.py, routers/audit_log.py, and
domain_helpers.py). Every helper this module needs (_decimal_to_float,
_shop_owner_map, _current_user_shops, _deposit_paid_for_shop,
_pending_rent_for_user) already lives in domain_helpers.py, so this router
has no dependency on app.py itself.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import Bill, Complex, DepositPayment, Payment, Shop, User, UserShop
from auth_service import require_admin
from domain_helpers import (
    _decimal_to_float, _shop_owner_map, _current_user_shops,
    _deposit_paid_for_shop, _pending_rent_for_user,
)

router = APIRouter(tags=["Reports"])


@router.get("/api/reports/summary", tags=["Reports"])
def reports_summary(
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """
    Business summary report for a date range (defaults to all-time if omitted).
    Includes: occupancy snapshot, collections, payments, and outstanding dues.
    Admin only.
    """
    # ── Occupancy snapshot (current state, not date-filtered — it's a point-in-time fact) ──
    shops = db.query(Shop).all()
    total_shops = len(shops)
    occupied = sum(1 for s in shops if s.status == "occupied")
    available = sum(1 for s in shops if s.status == "available")
    maintenance = sum(1 for s in shops if s.status == "maintenance")

    # ── Bills raised in range ──
    bill_q = db.query(Bill)
    if start_date:
        bill_q = bill_q.filter(Bill.bill_date >= start_date)
    if end_date:
        bill_q = bill_q.filter(Bill.bill_date <= end_date)
    bills_in_range = bill_q.all()

    total_billed = sum(_decimal_to_float(b.amount) for b in bills_in_range)
    total_pending_in_range = sum(_decimal_to_float(b.pending_amount) for b in bills_in_range)

    # ── Payments received in range (this is the actual "collections" figure) ──
    pay_q = db.query(Payment)
    if start_date:
        pay_q = pay_q.filter(Payment.payment_date >= start_date)
    if end_date:
        pay_q = pay_q.filter(Payment.payment_date <= end_date)
    payments_in_range = pay_q.all()
    total_collected = sum(_decimal_to_float(p.amount) for p in payments_in_range)

    by_method = {}
    for p in payments_in_range:
        by_method[p.payment_method] = by_method.get(p.payment_method, 0.0) + _decimal_to_float(p.amount)

    # ── Outstanding dues across ALL bills (current liability, not range-limited) ──
    all_bills = db.query(Bill).filter(Bill.status != "paid").order_by(Bill.bill_date).all()
    outstanding = [
        {
            "bill_id": b.id,
            "user_id": b.user_id,
            "shop_id": b.shop_id,
            "bill_type": b.bill_type,
            "description": b.description,
            "pending_amount": _decimal_to_float(b.pending_amount),
            "bill_date": b.bill_date,
            "due_date": b.due_date,
            "status": b.status,
        }
        for b in all_bills
    ]

    return {
        "range": {"start_date": start_date, "end_date": end_date},
        "occupancy": {
            "total_shops": total_shops,
            "occupied": occupied,
            "available": available,
            "maintenance": maintenance,
        },
        "collections": {
            "total_billed_in_range": round(total_billed, 2),
            "total_collected_in_range": round(total_collected, 2),
            "total_pending_in_range": round(total_pending_in_range, 2),
            "bills_raised_count": len(bills_in_range),
            "payments_received_count": len(payments_in_range),
            "collected_by_method": {k: round(v, 2) for k, v in by_method.items()},
        },
        "outstanding_dues": {
            "total_outstanding": round(sum(o["pending_amount"] for o in outstanding), 2),
            "bill_count": len(outstanding),
            "bills": outstanding,
        },
    }


@router.get("/api/reports/rent-collection", tags=["Reports"])
def report_rent_collection(
    complex_id: Optional[int] = None,
    user_id:    Optional[int] = None,
    shop_id:    Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    month:      Optional[int] = None,
    year:       Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status", pattern="^(paid|partial|pending)$"),
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """Rent collection report with optional filters. Admin only."""
    q = db.query(Bill).filter(Bill.bill_type == "Rent")
    if user_id is not None:
        q = q.filter(Bill.user_id == user_id)
    if shop_id is not None:
        q = q.filter(Bill.shop_id == shop_id)
    if complex_id is not None:
        q = q.join(Shop, Shop.id == Bill.shop_id).filter(Shop.complex_id == complex_id)
    if start_date is not None:
        q = q.filter(Bill.bill_date >= start_date)
    if end_date is not None:
        q = q.filter(Bill.bill_date <= end_date)
    if month is not None:
        q = q.filter(text("MONTH(bills.bill_date) = :m")).params(m=month)
    if year is not None:
        q = q.filter(text("YEAR(bills.bill_date) = :y")).params(y=year)
    if status_filter is not None:
        q = q.filter(Bill.status == status_filter)

    bills = q.order_by(Bill.bill_date).all()

    users = {u.id: u for u in db.query(User).all()}
    shops = {s.id: s for s in db.query(Shop).all()}
    complexes = {c.id: c.name for c in db.query(Complex).all()}

    records = []
    for b in bills:
        u = users.get(b.user_id)
        s = shops.get(b.shop_id)
        records.append({
            "bill_id": b.id, "user_id": b.user_id, "user_name": u.name if u else None,
            "mobile": u.mobile if u else None,
            "complex_id": s.complex_id if s else None,
            "complex_name": complexes.get(s.complex_id) if s else None,
            "shop_id": b.shop_id, "shop_number": s.shop_number if s else None,
            "bill_type": b.bill_type, "bill_date": b.bill_date, "due_date": b.due_date,
            "amount": _decimal_to_float(b.amount), "paid_amount": _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount), "status": b.status,
            "description": b.description,
        })

    return {
        "period": {"start_date": start_date, "end_date": end_date},
        "summary": {
            "total_billed": round(sum(r["amount"] for r in records), 2),
            "total_collected": round(sum(r["paid_amount"] for r in records), 2),
            "total_pending": round(sum(r["pending_amount"] for r in records), 2),
            "bills_count": len(records),
            "paid_count": sum(1 for r in records if r["status"] == "paid"),
            "partial_count": sum(1 for r in records if r["status"] == "partial"),
            "pending_count": sum(1 for r in records if r["status"] == "pending"),
        },
        "records": records,
    }


@router.get("/api/reports/deposit", tags=["Reports"])
def report_deposit(
    complex_id: Optional[int] = None,
    user_id:    Optional[int] = None,
    shop_id:    Optional[int] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """Deposit collection report. Admin only."""
    q = db.query(UserShop, User, Shop).join(User, User.id == UserShop.user_id).join(Shop, Shop.id == UserShop.shop_id)
    if user_id is not None:
        q = q.filter(UserShop.user_id == user_id)
    if shop_id is not None:
        q = q.filter(UserShop.shop_id == shop_id)
    if complex_id is not None:
        q = q.filter(Shop.complex_id == complex_id)

    rows = q.order_by(UserShop.user_id).all()
    complexes = {c.id: c.name for c in db.query(Complex).all()}

    records = []
    full_count = partial_count = none_count = 0
    total_required = total_paid = 0.0

    for us, u, s in rows:
        required = _decimal_to_float(s.shop_deposit)
        paid = _deposit_paid_for_shop(db, u.id, s.id)
        remaining = max(0.0, required - paid)
        last_dp = (
            db.query(DepositPayment)
            .filter(DepositPayment.user_id == u.id, DepositPayment.shop_id == s.id)
            .order_by(DepositPayment.payment_date.desc())
            .first()
        )
        if paid >= required and required > 0:
            dep_status = "full"
            full_count += 1
        elif paid > 0:
            dep_status = "partial"
            partial_count += 1
        else:
            dep_status = "none"
            none_count += 1

        total_required += required
        total_paid += paid

        records.append({
            "user_id": u.id, "user_name": u.name, "mobile": u.mobile,
            "complex_name": complexes.get(s.complex_id),
            "shop_id": s.id, "shop_number": s.shop_number,
            "deposit_required": round(required, 2), "deposit_paid": round(paid, 2),
            "deposit_remaining": round(remaining, 2), "deposit_status": dep_status,
            "last_deposit_date": last_dp.payment_date if last_dp else None,
        })

    return {
        "summary": {
            "total_deposit_required": round(total_required, 2),
            "total_deposit_collected": round(total_paid, 2),
            "total_deposit_remaining": round(total_required - total_paid, 2),
            "tenants_with_full_deposit": full_count,
            "tenants_with_partial_deposit": partial_count,
            "tenants_with_no_deposit": none_count,
        },
        "records": records,
    }


@router.get("/api/reports/occupancy", tags=["Reports"])
def report_occupancy(
    complex_id: Optional[int] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """Occupancy report, overall and broken down by complex. Admin only."""
    shop_q = db.query(Shop)
    if complex_id is not None:
        shop_q = shop_q.filter(Shop.complex_id == complex_id)
    shops = shop_q.order_by(Shop.id).all()

    complexes = {c.id: c for c in db.query(Complex).all()}
    owner_map = _shop_owner_map(db, [s.id for s in shops]) if shops else {}

    total_shops = len(shops)
    occupied = sum(1 for s in shops if s.status == "occupied")
    available = total_shops - occupied
    occupancy_rate = round((occupied / total_shops) * 100) if total_shops else 0

    by_complex_data = {}
    shop_details = []
    for s in shops:
        cdata = by_complex_data.setdefault(s.complex_id, {
            "complex_id": s.complex_id,
            "complex_name": complexes.get(s.complex_id).name if s.complex_id and complexes.get(s.complex_id) else None,
            "total_shops": 0, "occupied": 0, "available": 0,
            "monthly_rent_potential": 0.0, "monthly_rent_actual": 0.0,
        })
        cdata["total_shops"] += 1
        rent = _decimal_to_float(s.shop_rent)
        cdata["monthly_rent_potential"] += rent
        owner = owner_map.get(s.id)

        if s.status == "occupied":
            cdata["occupied"] += 1
            cdata["monthly_rent_actual"] += rent  # use current shop rent
        else:
            cdata["available"] += 1

        shop_details.append({
            "shop_id": s.id, "shop_number": s.shop_number, "complex_id": s.complex_id,
            "complex_name": cdata["complex_name"],
            "status": s.status, "area_sqft": _decimal_to_float(s.area_sqft),
            "shop_rent": rent, "shop_deposit": _decimal_to_float(s.shop_deposit),
            "tenant_id": owner.id if owner else None,
            "tenant_name": owner.name if owner else None,
            "tenant_mobile": owner.mobile if owner else None,
        })

    by_complex = []
    for cdata in by_complex_data.values():
        ct = cdata["total_shops"]
        cdata["occupancy_rate_percent"] = round((cdata["occupied"] / ct) * 100) if ct else 0
        cdata["monthly_rent_potential"] = round(cdata["monthly_rent_potential"], 2)
        cdata["monthly_rent_actual"] = round(cdata["monthly_rent_actual"], 2)
        by_complex.append(cdata)

    return {
        "summary": {
            "total_shops": total_shops, "occupied": occupied, "available": available,
            "occupancy_rate_percent": occupancy_rate,
        },
        "by_complex": by_complex,
        "shop_details": shop_details,
    }


@router.get("/api/reports/user-wise", tags=["Reports"])
def report_user_wise(
    complex_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    month:      Optional[int] = None,
    year:       Optional[int] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """User-wise financial report. Admin only."""
    users = db.query(User).filter(User.role == "tenant").order_by(User.id).all()
    complexes = {c.id: c.name for c in db.query(Complex).all()}

    # Prefetched once instead of once per user/shop below (this used to be
    # N+1 - a Shop lookup AND a deposit-sum query for every shop of every
    # tenant). Same figures as _deposit_paid_for_shop/the old per-shop
    # query, just grouped in Python instead of re-queried in the loop.
    shops_by_id = {s.id: s for s in db.query(Shop).all()}
    deposits_by_user_shop = {}
    for dp in db.query(DepositPayment).all():
        key = (dp.user_id, dp.shop_id)
        deposits_by_user_shop[key] = deposits_by_user_shop.get(key, 0.0) + _decimal_to_float(dp.amount)

    results = []
    for u in users:
        user_shops = _current_user_shops(db, u.id)
        if complex_id is not None:
            shop_ids_in_complex = {s.id for s in shops_by_id.values() if s.complex_id == complex_id}
            user_shops = [us for us in user_shops if us.shop_id in shop_ids_in_complex]
            if not user_shops:
                continue

        shops_list = []
        deposit_required = deposit_paid = 0.0
        for us in user_shops:
            shop = shops_by_id.get(us.shop_id)
            if not shop:
                continue
            shops_list.append({
                "shop_number": shop.shop_number,
                "complex_name": complexes.get(shop.complex_id),
                "shop_rent": _decimal_to_float(shop.shop_rent),  # <-- DIRECT
                "shop_deposit": _decimal_to_float(shop.shop_deposit),
            })
            deposit_required += _decimal_to_float(shop.shop_deposit)
            deposit_paid += deposits_by_user_shop.get((u.id, shop.id), 0.0)

        bill_q = db.query(Bill).filter(Bill.user_id == u.id)
        if start_date is not None:
            bill_q = bill_q.filter(Bill.bill_date >= start_date)
        if end_date is not None:
            bill_q = bill_q.filter(Bill.bill_date <= end_date)
        if month is not None:
            bill_q = bill_q.filter(text("MONTH(bills.bill_date) = :m")).params(m=month)
        if year is not None:
            bill_q = bill_q.filter(text("YEAR(bills.bill_date) = :y")).params(y=year)
        bills = bill_q.all()

        if not bills and not shops_list:
            continue

        total_billed = sum(_decimal_to_float(b.amount) for b in bills)
        total_collected = sum(_decimal_to_float(b.paid_amount) for b in bills)
        total_pending = sum(_decimal_to_float(b.pending_amount) for b in bills)

        last_payment = (
            db.query(Payment).join(Bill, Bill.id == Payment.bill_id)
            .filter(Bill.user_id == u.id).order_by(Payment.payment_date.desc()).first()
        )
        payment_count = db.query(Payment).join(Bill, Bill.id == Payment.bill_id).filter(Bill.user_id == u.id).count()

        results.append({
            "user_id": u.id, "user_name": u.name, "mobile": u.mobile,
            "email": u.email, "is_active": u.is_active,
            "shops": shops_list,
            "total_billed": round(total_billed, 2),
            "total_collected": round(total_collected, 2),
            "total_pending": round(total_pending, 2),
            "deposit_required": round(deposit_required, 2),
            "deposit_paid": round(deposit_paid, 2),
            "deposit_remaining": round(deposit_required - deposit_paid, 2),
            "payment_count": payment_count,
            "last_payment_date": last_payment.payment_date if last_payment else None,
        })

    return results


@router.get("/api/reports/business-overview", tags=["Reports"])
def report_business_overview(
    complex_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """
    Consolidated business-health report: collection efficiency, overdue aging buckets,
    complex-wise performance comparison, top defaulters, and a 6-month collection trend.
    Admin only.
    """
    today = datetime.now(timezone.utc).date()

    # ── Base bill query (optionally scoped to a complex), used for range figures ──
    def _bills_query(q):
        if complex_id is not None:
            q = q.join(Shop, Shop.id == Bill.shop_id).filter(Shop.complex_id == complex_id)
        return q

    bill_q = _bills_query(db.query(Bill))
    if start_date is not None:
        bill_q = bill_q.filter(Bill.bill_date >= start_date)
    if end_date is not None:
        bill_q = bill_q.filter(Bill.bill_date <= end_date)
    bills_in_range = bill_q.all()

    total_billed = sum(_decimal_to_float(b.amount) for b in bills_in_range)
    total_collected_of_range_bills = sum(_decimal_to_float(b.paid_amount) for b in bills_in_range)
    collection_efficiency_percent = round((total_collected_of_range_bills / total_billed) * 100, 1) if total_billed else 0.0

    # ── Overdue aging buckets (based on ALL unpaid bills, not range-limited — it's a liability snapshot) ──
    unpaid_q = _bills_query(db.query(Bill)).filter(Bill.status != "paid")
    unpaid_bills = unpaid_q.all()
    users = {u.id: u for u in db.query(User).all()}
    shops = {s.id: s for s in db.query(Shop).all()}
    complexes = {c.id: c.name for c in db.query(Complex).all()}

    buckets = {"current": 0.0, "0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "90_plus": 0.0}
    bucket_counts = {"current": 0, "0_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0}
    for b in unpaid_bills:
        pending = _decimal_to_float(b.pending_amount)
        due = b.due_date.date() if isinstance(b.due_date, datetime) else b.due_date
        days_overdue = (today - due).days if due else 0
        if days_overdue <= 0:
            key = "current"
        elif days_overdue <= 30:
            key = "0_30"
        elif days_overdue <= 60:
            key = "31_60"
        elif days_overdue <= 90:
            key = "61_90"
        else:
            key = "90_plus"
        buckets[key] += pending
        bucket_counts[key] += 1
    buckets = {k: round(v, 2) for k, v in buckets.items()}

    # ── Complex-wise performance comparison ──
    all_shops = db.query(Shop).all() if complex_id is None else db.query(Shop).filter(Shop.complex_id == complex_id).all()
    by_complex = {}
    for s in all_shops:
        cdata = by_complex.setdefault(s.complex_id, {
            "complex_id": s.complex_id,
            "complex_name": complexes.get(s.complex_id),
            "total_shops": 0, "occupied": 0,
            "billed": 0.0, "collected": 0.0,
        })
        cdata["total_shops"] += 1
        if s.status == "occupied":
            cdata["occupied"] += 1
    for b in bills_in_range:
        s = shops.get(b.shop_id)
        if not s or s.complex_id not in by_complex:
            continue
        by_complex[s.complex_id]["billed"] += _decimal_to_float(b.amount)
        by_complex[s.complex_id]["collected"] += _decimal_to_float(b.paid_amount)

    complex_performance = []
    for cdata in by_complex.values():
        ct = cdata["total_shops"]
        billed = cdata["billed"]
        complex_performance.append({
            "complex_id": cdata["complex_id"],
            "complex_name": cdata["complex_name"],
            "total_shops": ct,
            "occupied": cdata["occupied"],
            "occupancy_rate_percent": round((cdata["occupied"] / ct) * 100) if ct else 0,
            "billed": round(billed, 2),
            "collected": round(cdata["collected"], 2),
            "collection_rate_percent": round((cdata["collected"] / billed) * 100, 1) if billed else 0.0,
        })
    complex_performance.sort(key=lambda r: r["collection_rate_percent"])

    # ── Top defaulters (highest pending amount, current unpaid bills only) ──
    defaulter_totals = {}
    for b in unpaid_bills:
        defaulter_totals.setdefault(b.user_id, 0.0)
        defaulter_totals[b.user_id] += _decimal_to_float(b.pending_amount)
    top_defaulters = []
    for uid, pending in sorted(defaulter_totals.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        u = users.get(uid)
        oldest_due = min(
            (b.due_date for b in unpaid_bills if b.user_id == uid and b.due_date),
            default=None,
        )
        top_defaulters.append({
            "user_id": uid,
            "user_name": u.name if u else None,
            "mobile": u.mobile if u else None,
            "total_pending": round(pending, 2),
            "oldest_due_date": oldest_due,
        })

    # ── 6-month collection trend (billed vs collected, by calendar month) ──
    trend = []
    for i in range(5, -1, -1):
        ref = today.replace(day=1)
        # step back i months
        y, m = ref.year, ref.month - i
        while m <= 0:
            m += 12
            y -= 1
        m_start = datetime(y, m, 1, tzinfo=timezone.utc)
        m_end_year, m_end_month = (y + 1, 1) if m == 12 else (y, m + 1)
        m_end = datetime(m_end_year, m_end_month, 1, tzinfo=timezone.utc)

        m_bill_q = _bills_query(db.query(Bill)).filter(Bill.bill_date >= m_start, Bill.bill_date < m_end)
        m_billed = sum(_decimal_to_float(b.amount) for b in m_bill_q.all())

        m_pay_q = db.query(Payment).filter(Payment.payment_date >= m_start, Payment.payment_date < m_end)
        if complex_id is not None:
            m_pay_q = m_pay_q.join(Bill, Bill.id == Payment.bill_id).join(Shop, Shop.id == Bill.shop_id).filter(Shop.complex_id == complex_id)
        m_collected = sum(_decimal_to_float(p.amount) for p in m_pay_q.all())

        trend.append({
            "month": m_start.strftime("%b %Y"),
            "billed": round(m_billed, 2),
            "collected": round(m_collected, 2),
        })

    # ── Daily-ops snapshot: today's collections, bills due today / this week ──
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    week_end = day_start + timedelta(days=7)

    today_payments = db.query(Payment).filter(Payment.payment_date >= day_start, Payment.payment_date < day_end).all()
    collections_today = round(sum(_decimal_to_float(p.amount) for p in today_payments), 2)

    due_today = [b for b in unpaid_bills if b.due_date and day_start.date() <= (b.due_date.date() if isinstance(b.due_date, datetime) else b.due_date) < day_end.date()]
    due_this_week = [b for b in unpaid_bills if b.due_date and today <= (b.due_date.date() if isinstance(b.due_date, datetime) else b.due_date) <= week_end.date()]

    today_snapshot = {
        "collections_today": collections_today,
        "payments_today_count": len(today_payments),
        "due_today_amount": round(sum(_decimal_to_float(b.pending_amount) for b in due_today), 2),
        "due_today_count": len(due_today),
        "due_this_week_amount": round(sum(_decimal_to_float(b.pending_amount) for b in due_this_week), 2),
        "due_this_week_count": len(due_this_week),
        "overdue_amount": round(sum(v for k, v in buckets.items() if k != "current"), 2),
        "overdue_count": sum(c for k, c in bucket_counts.items() if k != "current"),
    }

    return {
        "range": {"start_date": start_date, "end_date": end_date},
        "today_snapshot": today_snapshot,
        "collection_efficiency": {
            "total_billed_in_range": round(total_billed, 2),
            "total_collected_in_range": round(total_collected_of_range_bills, 2),
            "collection_efficiency_percent": collection_efficiency_percent,
        },
        "aging": {
            "buckets": buckets,
            "bucket_counts": bucket_counts,
            "total_outstanding": round(sum(buckets.values()), 2),
        },
        "complex_performance": complex_performance,
        "top_defaulters": top_defaulters,
        "monthly_trend": trend,
    }


@router.get("/api/reports/tenant-statement", tags=["Reports"])
def report_tenant_statement(
    user_id:    int,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """
    Full bill + payment ledger for one tenant — every bill they've ever been
    raised, each bill's payments, sorted chronologically by bill_date, so the
    tenant can see month-by-month exactly what's paid and what's pending.
    Admin only.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    bill_q = db.query(Bill).filter(Bill.user_id == user_id)
    if start_date is not None:
        bill_q = bill_q.filter(Bill.bill_date >= start_date)
    if end_date is not None:
        bill_q = bill_q.filter(Bill.bill_date <= end_date)
    bills = bill_q.order_by(Bill.bill_date).all()

    shops = {s.id: s for s in db.query(Shop).all()}
    complexes = {c.id: c.name for c in db.query(Complex).all()}
    bill_ids = [b.id for b in bills]
    payments = db.query(Payment).filter(Payment.bill_id.in_(bill_ids)).order_by(Payment.payment_date).all() if bill_ids else []
    payments_by_bill = {}
    for p in payments:
        payments_by_bill.setdefault(p.bill_id, []).append(p)

    ledger = []
    for b in bills:
        s = shops.get(b.shop_id)
        bd = b.bill_date.date() if isinstance(b.bill_date, datetime) else b.bill_date
        ledger.append({
            "bill_id": b.id,
            "bill_month": bd.strftime("%b %Y") if bd else None,
            "bill_date": b.bill_date,
            "due_date": b.due_date,
            "bill_type": b.bill_type,
            "description": b.description,
            "shop_number": s.shop_number if s else None,
            "complex_name": complexes.get(s.complex_id) if s else None,
            "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "status": b.status,
            "payments": [
                {
                    "payment_id": p.id,
                    "payment_date": p.payment_date,
                    "amount": _decimal_to_float(p.amount),
                    "payment_method": p.payment_method,
                }
                for p in payments_by_bill.get(b.id, [])
            ],
        })

    total_billed = sum(x["amount"] for x in ledger)
    total_paid = sum(x["paid_amount"] for x in ledger)
    total_pending = sum(x["pending_amount"] for x in ledger)

    return {
        "user": {"id": user.id, "name": user.name, "mobile": user.mobile, "email": user.email},
        "range": {"start_date": start_date, "end_date": end_date},
        "summary": {
            "total_billed": round(total_billed, 2),
            "total_paid": round(total_paid, 2),
            "total_pending": round(total_pending, 2),
            "bills_count": len(ledger),
            "paid_count": sum(1 for x in ledger if x["status"] == "paid"),
            "pending_count": sum(1 for x in ledger if x["status"] in ("pending", "partial")),
        },
        "ledger": ledger,
    }


@router.get("/api/finance/overview", tags=["Reports"])
def finance_overview(
    complex_id: Optional[int] = None,
    user_id:    Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    month:      Optional[int] = None,
    year:       Optional[int] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """Aggregated finance overview with filters. Powers the Finance module page. Admin only."""
    bill_q = db.query(Bill)
    if user_id is not None:
        bill_q = bill_q.filter(Bill.user_id == user_id)
    if complex_id is not None:
        bill_q = bill_q.join(Shop, Shop.id == Bill.shop_id).filter(Shop.complex_id == complex_id)
    if start_date is not None:
        bill_q = bill_q.filter(Bill.bill_date >= start_date)
    if end_date is not None:
        bill_q = bill_q.filter(Bill.bill_date <= end_date)
    if month is not None:
        bill_q = bill_q.filter(text("MONTH(bills.bill_date) = :m")).params(m=month)
    if year is not None:
        bill_q = bill_q.filter(text("YEAR(bills.bill_date) = :y")).params(y=year)

    rent_bills = bill_q.filter(Bill.bill_type == "Rent").all()
    total_rent_billed = sum(_decimal_to_float(b.amount) for b in rent_bills)
    total_rent_collected = sum(_decimal_to_float(b.paid_amount) for b in rent_bills)
    total_rent_pending = sum(_decimal_to_float(b.pending_amount) for b in rent_bills)

    # Deposit figures are point-in-time (not date filtered), scoped by complex/user if given
    us_q = db.query(UserShop, User, Shop).join(User, User.id == UserShop.user_id).join(Shop, Shop.id == UserShop.shop_id)
    if user_id is not None:
        us_q = us_q.filter(UserShop.user_id == user_id)
    if complex_id is not None:
        us_q = us_q.filter(Shop.complex_id == complex_id)
    us_rows = us_q.all()

    complexes = {c.id: c.name for c in db.query(Complex).all()}
    tenants_map = {}
    total_deposit_required = total_deposit_collected = 0.0

    for us, u, s in us_rows:
        deposit_required = _decimal_to_float(s.shop_deposit)
        deposit_paid = _deposit_paid_for_shop(db, u.id, s.id)
        total_deposit_required += deposit_required
        total_deposit_collected += deposit_paid

        entry = tenants_map.setdefault(u.id, {
            "user_id": u.id, "user_name": u.name, "mobile": u.mobile,
            "complex_name": complexes.get(s.complex_id), "shops": [],
            "monthly_rent": 0.0, "rent_pending": 0.0,
            "deposit_required": 0.0, "deposit_paid": 0.0,
            "last_payment_date": None, "outstanding_balance": 0.0,
        })
        entry["shops"].append(s.shop_number)
        entry["monthly_rent"] += _decimal_to_float(s.shop_rent)  # <-- DIRECT
        entry["deposit_required"] += deposit_required
        entry["deposit_paid"] += deposit_paid

    for uid, entry in tenants_map.items():
        entry["rent_pending"] = round(_pending_rent_for_user(db, uid), 2)
        entry["outstanding_balance"] = entry["rent_pending"]
        entry["deposit_remaining"] = round(entry["deposit_required"] - entry["deposit_paid"], 2)
        entry["monthly_rent"] = round(entry["monthly_rent"], 2)
        entry["deposit_required"] = round(entry["deposit_required"], 2)
        entry["deposit_paid"] = round(entry["deposit_paid"], 2)
        last_payment = (
            db.query(Payment).join(Bill, Bill.id == Payment.bill_id)
            .filter(Bill.user_id == uid).order_by(Payment.payment_date.desc()).first()
        )
        entry["last_payment_date"] = last_payment.payment_date if last_payment else None

    pay_q = db.query(Payment).join(Bill, Bill.id == Payment.bill_id)
    if user_id is not None:
        pay_q = pay_q.filter(Bill.user_id == user_id)
    if complex_id is not None:
        pay_q = pay_q.join(Shop, Shop.id == Bill.shop_id).filter(Shop.complex_id == complex_id)
    if start_date is not None:
        pay_q = pay_q.filter(Payment.payment_date >= start_date)
    if end_date is not None:
        pay_q = pay_q.filter(Payment.payment_date <= end_date)
    recent_payments_rows = pay_q.order_by(Payment.payment_date.desc()).limit(20).all()

    users_by_id = {u.id: u for u in db.query(User).all()}
    shops_by_id = {s.id: s for s in db.query(Shop).all()}

    recent_payments = []
    for p in recent_payments_rows:
        bill = p.bill
        u = users_by_id.get(bill.user_id) if bill else None
        s = shops_by_id.get(bill.shop_id) if bill else None
        recent_payments.append({
            "id": p.id, "user_id": bill.user_id if bill else None,
            "user_name": u.name if u else None,
            "shop_number": s.shop_number if s else None,
            "bill_type": bill.bill_type if bill else None,
            "amount": _decimal_to_float(p.amount), "payment_method": p.payment_method,
            "payment_date": p.payment_date, "remarks": p.remarks or "",
        })

    dp_q = db.query(DepositPayment)
    if user_id is not None:
        dp_q = dp_q.filter(DepositPayment.user_id == user_id)
    if complex_id is not None:
        dp_q = dp_q.join(Shop, Shop.id == DepositPayment.shop_id).filter(Shop.complex_id == complex_id)
    deposit_payment_count = dp_q.count()
    recent_deposit_rows = dp_q.order_by(DepositPayment.payment_date.desc()).limit(20).all()
    recent_deposit_payments = [
        {
            "id": dp.id, "user_id": dp.user_id,
            "user_name": users_by_id.get(dp.user_id).name if users_by_id.get(dp.user_id) else None,
            "shop_number": shops_by_id.get(dp.shop_id).shop_number if shops_by_id.get(dp.shop_id) else None,
            "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
            "remarks": dp.remarks,
        }
        for dp in recent_deposit_rows
    ]

    return {
        "filters_applied": {
            "complex_id": complex_id, "user_id": user_id,
            "start_date": start_date, "end_date": end_date,
            "month": month, "year": year,
        },
        "summary": {
            "total_rent_billed": round(total_rent_billed, 2),
            "total_rent_collected": round(total_rent_collected, 2),
            "total_rent_pending": round(total_rent_pending, 2),
            "total_deposit_required": round(total_deposit_required, 2),
            "total_deposit_collected": round(total_deposit_collected, 2),
            "total_deposit_remaining": round(total_deposit_required - total_deposit_collected, 2),
            "payment_count": pay_q.count(),
            "deposit_payment_count": deposit_payment_count,
        },
        "tenants": list(tenants_map.values()),
        "recent_payments": recent_payments,
        "recent_deposit_payments": recent_deposit_payments,
    }
