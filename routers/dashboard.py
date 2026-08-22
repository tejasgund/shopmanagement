"""
routers/dashboard.py - GET /api/dashboard/kpis (Admin only).

Extracted verbatim from app.py (step 6b of the router/service split).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import AuditLog, Bill, DepositPayment, Payment, Shop, User
from auth_service import require_admin
from domain_helpers import _decimal_to_float

router = APIRouter(tags=["Dashboard"])


@router.get("/api/dashboard/kpis", tags=["Dashboard"])
def dashboard_kpis(
    db: Session = Depends(get_db),
    _:  User    = Depends(require_admin),
):
    """
    Single endpoint that returns all top-level KPI numbers needed for the
    admin dashboard home page.  Eliminates multiple sequential API calls
    from the frontend.  Admin only.

    Response includes:
        tenants_total, tenants_active
        shops_total, shops_occupied, shops_available, shops_maintenance
        bills_pending_count, bills_pending_amount
        collections_this_month
        deposit_collected_total, deposit_required_total
        recent_activity  – last 5 audit log entries
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Tenants
    tenants_total  = db.query(User).filter(User.role == "tenant").count()
    tenants_active = db.query(User).filter(User.role == "tenant", User.is_active == True).count()

    # Shops
    shop_counts = dict(db.query(Shop.status, func.count(Shop.id)).group_by(Shop.status).all())
    shops_total       = sum(shop_counts.values())
    shops_occupied    = shop_counts.get("occupied", 0)
    shops_available   = shop_counts.get("available", 0)
    shops_maintenance = shop_counts.get("maintenance", 0)

    # Pending bills
    pending_bills = db.query(Bill).filter(Bill.status.in_(["pending", "partial"])).all()
    bills_pending_count  = len(pending_bills)
    bills_pending_amount = round(sum(_decimal_to_float(b.pending_amount) for b in pending_bills), 2)

    # Collections this month
    month_payments = (
        db.query(Payment)
        .filter(Payment.payment_date >= month_start)
        .all()
    )
    collections_this_month = round(sum(_decimal_to_float(p.amount) for p in month_payments), 2)

    # Deposits
    deposit_required_total = round(
        sum(_decimal_to_float(s.shop_deposit) for s in db.query(Shop).filter(Shop.status == "occupied").all()), 2
    )
    deposit_collected_total = round(
        sum(_decimal_to_float(r.amount) for r in db.query(DepositPayment).all()), 2
    )

    # Recent activity (last 5 audit log entries)
    recent_rows = (
        db.query(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(5)
        .all()
    )
    recent_activity = [
        {
            "id":         log.id,
            "action":     log.action,
            "table_name": log.table_name,
            "record_id":  log.record_id,
            "created_at": log.created_at,
            "actor":      user.name if user else "Unknown",
        }
        for log, user in recent_rows
    ]

    return {
        "success": True,
        "tenants": {"total": tenants_total, "active": tenants_active},
        "shops": {
            "total":       shops_total,
            "occupied":    shops_occupied,
            "available":   shops_available,
            "maintenance": shops_maintenance,
        },
        "bills": {
            "pending_count":  bills_pending_count,
            "pending_amount": bills_pending_amount,
        },
        "collections_this_month": collections_this_month,
        "deposits": {
            "required":  deposit_required_total,
            "collected": deposit_collected_total,
            "remaining": round(deposit_required_total - deposit_collected_total, 2),
        },
        "recent_activity": recent_activity,
    }
