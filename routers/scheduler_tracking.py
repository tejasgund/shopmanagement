"""
routers/scheduler_tracking.py - the Scheduler tracking API (admin only).

READ-ONLY, apart from the penalty settings. The schedulers themselves are two
standalone scripts under scheduler/ that talk to the database and nothing
else; this module reads back what they recorded. Nothing here can start a run
- cron owns that, so there is exactly one thing that decides when a scheduler
executes.

    scripts  ->  scheduler_runs / scheduler_run_items  ->  this API  ->  frontend

Every endpoint reads the two tracking tables. It never recomputes a penalty or
re-derives a rent figure: the `reason` column already holds the explanation
the script wrote at the moment it decided, in the settings that applied then.
Recomputing from today's settings would produce an explanation that quietly
disagrees with the charge actually made.
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from core.database import get_db
from core.security import require_admin
from models.schema import Bill, SchedulerRun, SchedulerRunItem, Shop, User
from schemas.api import SettingsUpdateRequest
from services import settings as settings_service
from services.audit import write_audit

router = APIRouter(tags=["Scheduler"])

# The two scripts, as they name themselves in the tracking tables.
SCHEDULERS = {
    "auto_rent_generation": {
        "label": "Auto Rent Generation",
        "description": "Creates each tenant's monthly Rent bill on their rent day.",
        "script": "scheduler/auto_rent_generation/auto_rent_generation.py",
    },
    "due_bill_penalty": {
        "label": "Due Bill Penalty",
        "description": "Applies the daily late fee to overdue unpaid bills.",
        "script": "scheduler/due_bill_penalty/due_bill_penalty.py",
    },
}

# A run that started this long ago and never finished was almost certainly
# killed - the box rebooted, the process was cut off - rather than still
# working. Shown as stalled so it is not mistaken for a run in progress.
STALLED_AFTER = timedelta(hours=2)


# ══════════════════════════════════════════════════════════════════════════════
# SERIALISERS
# ══════════════════════════════════════════════════════════════════════════════

def _float(value) -> Optional[float]:
    return float(value) if value is not None else None


def _run_dict(run: SchedulerRun, now: datetime) -> dict:
    spec = SCHEDULERS.get(run.scheduler, {})
    stalled = (
        run.status == "RUNNING"
        and run.started_at is not None
        and run.started_at < (now - STALLED_AFTER)
    )
    return {
        "run_id": run.run_id,
        "scheduler": run.scheduler,
        "scheduler_label": spec.get("label", run.scheduler),
        "run_date": run.run_date,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": run.duration_ms,
        "status": run.status,
        "is_stalled": stalled,
        "trigger_source": run.trigger_source,
        "items_total": run.items_total,
        "items_succeeded": run.items_succeeded,
        "items_failed": run.items_failed,
        "items_skipped": run.items_skipped,
        "amount_total": _float(run.amount_total),
        "error_message": run.error_message,
        "hostname": run.hostname,
    }


def _item_dict(item: SchedulerRunItem) -> dict:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "scheduler": item.scheduler,
        "run_date": item.run_date,
        "user_id": item.user_id,
        "user_name": item.user_name,
        "shop_id": item.shop_id,
        "shop_number": item.shop_number,
        "bill_id": item.bill_id,
        "action": item.action,
        "status": item.status,
        "amount": _float(item.amount),
        "penalty_amount": _float(item.penalty_amount),
        "penalty_days": item.penalty_days,
        "penalty_rate": _float(item.penalty_rate),
        "bill_due_date": item.bill_due_date,
        "period_key": item.period_key,
        # The sentence the script wrote when it decided. This is the answer to
        # "why", and it is stored rather than recomputed on purpose.
        "reason": item.reason,
        "error_message": item.error_message,
        "created_at": item.created_at,
    }


def _apply_date_range(query, column, date_from: Optional[date], date_to: Optional[date]):
    if date_from:
        query = query.filter(column >= date_from)
    if date_to:
        query = query.filter(column <= date_to)
    return query


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/scheduler/summary", tags=["Scheduler"])
def scheduler_summary(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    The headline: for each scheduler, when it last ran and whether it worked.

    "Last run" is deliberately separate from "last successful run". A
    scheduler that ran an hour ago and failed looks healthy by the first
    measure and is not, which is exactly the case this screen exists to catch.
    """
    now = datetime.now()
    out = []

    for name, spec in SCHEDULERS.items():
        base = db.query(SchedulerRun).filter(SchedulerRun.scheduler == name)
        last = base.order_by(SchedulerRun.started_at.desc()).first()
        last_ok = (
            base.filter(SchedulerRun.status == "SUCCESS")
            .order_by(SchedulerRun.started_at.desc())
            .first()
        )
        failed_7d = base.filter(
            SchedulerRun.status.in_(["FAILED", "PARTIAL"]),
            SchedulerRun.started_at >= now - timedelta(days=7),
        ).count()

        out.append({
            "scheduler": name,
            "label": spec["label"],
            "description": spec["description"],
            "script": spec["script"],
            "enabled": bool(settings_service.get_all(db).get(
                "scheduler.rent_generation_enabled" if name == "auto_rent_generation"
                else "scheduler.penalty_enabled"
            )),
            "last_run": _run_dict(last, now) if last else None,
            "last_successful_run": _run_dict(last_ok, now) if last_ok else None,
            "failed_runs_last_7_days": failed_7d,
            "never_run": last is None,
        })

    return {"generated_at": now, "schedulers": out}


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION HISTORY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/scheduler/runs", tags=["Scheduler"])
def list_runs(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    scheduler: Optional[str] = Query(None, description="auto_rent_generation | due_bill_penalty"),
    status: Optional[str] = Query(None, description="RUNNING | SUCCESS | PARTIAL | FAILED"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Execution history, newest first, with the filters the dashboard offers."""
    query = db.query(SchedulerRun)
    if scheduler:
        query = query.filter(SchedulerRun.scheduler == scheduler)
    if status:
        query = query.filter(SchedulerRun.status == status.upper())
    query = _apply_date_range(query, SchedulerRun.run_date, date_from, date_to)

    total = query.count()
    rows = (
        query.order_by(SchedulerRun.started_at.desc(), SchedulerRun.id.desc())
        .offset(offset).limit(limit).all()
    )
    now = datetime.now()
    return {"total": total, "limit": limit, "offset": offset,
            "runs": [_run_dict(r, now) for r in rows]}


@router.get("/api/scheduler/runs/{run_id}", tags=["Scheduler"])
def run_detail(
    run_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    action: Optional[str] = Query(None, description="filter the items by action"),
):
    """
    One run in full: its summary plus every customer/bill it touched.

    Reached by run_id rather than a numeric id because run_id is what appears
    in the logs and in the tracking rows - the string someone actually has in
    front of them when they come looking.
    """
    run = db.query(SchedulerRun).filter(SchedulerRun.run_id == run_id).first()
    if not run:
        raise HTTPException(404, detail=f"No run with id {run_id}")

    items_q = db.query(SchedulerRunItem).filter(SchedulerRunItem.run_id == run_id)
    if action:
        items_q = items_q.filter(SchedulerRunItem.action == action.upper())
    items = items_q.order_by(SchedulerRunItem.id).all()

    breakdown = dict(
        db.query(SchedulerRunItem.action, func.count(SchedulerRunItem.id))
        .filter(SchedulerRunItem.run_id == run_id)
        .group_by(SchedulerRunItem.action).all()
    )

    return {
        "run": _run_dict(run, datetime.now()),
        "action_breakdown": breakdown,
        "items": [_item_dict(i) for i in items],
    }


# ══════════════════════════════════════════════════════════════════════════════
# ITEM SEARCH  (rent tracking, penalty tracking, customer filtering)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/scheduler/items", tags=["Scheduler"])
def list_items(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    scheduler: Optional[str] = Query(None),
    action: Optional[str] = Query(None, description="RENT_CREATED, SKIPPED_DUPLICATE, PENALTY_APPLIED, ..."),
    status: Optional[str] = Query(None, description="SUCCESS | SKIPPED | FAILED"),
    user_id: Optional[int] = Query(None, description="customer filter"),
    shop_id: Optional[int] = Query(None),
    bill_id: Optional[int] = Query(None),
    period_key: Optional[str] = Query(None, description="e.g. RENT-2026-09"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    Everything the schedulers did to individual customers and bills.

    This one endpoint backs the Rent tab, the Penalty tab and the customer
    lookup - they are the same question with different filters, and giving
    them separate endpoints would mean three places to fix a bug in.
    """
    query = db.query(SchedulerRunItem)
    if scheduler:
        query = query.filter(SchedulerRunItem.scheduler == scheduler)
    if action:
        query = query.filter(SchedulerRunItem.action == action.upper())
    if status:
        query = query.filter(SchedulerRunItem.status == status.upper())
    if user_id:
        query = query.filter(SchedulerRunItem.user_id == user_id)
    if shop_id:
        query = query.filter(SchedulerRunItem.shop_id == shop_id)
    if bill_id:
        query = query.filter(SchedulerRunItem.bill_id == bill_id)
    if period_key:
        query = query.filter(SchedulerRunItem.period_key == period_key)
    query = _apply_date_range(query, SchedulerRunItem.run_date, date_from, date_to)

    total = query.count()
    rows = (
        query.order_by(SchedulerRunItem.run_date.desc(), SchedulerRunItem.id.desc())
        .offset(offset).limit(limit).all()
    )
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_item_dict(i) for i in rows]}


@router.get("/api/scheduler/customers/{user_id}", tags=["Scheduler"])
def customer_tracking(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    limit: int = Query(200, ge=1, le=1000),
):
    """
    Everything both schedulers have done for one tenant, newest first.

    The answer to "what has the system been doing to this customer" in one
    call, including the months it decided NOT to bill them and why.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="Tenant not found")

    rows = (
        db.query(SchedulerRunItem)
        .filter(SchedulerRunItem.user_id == user_id)
        .order_by(SchedulerRunItem.run_date.desc(), SchedulerRunItem.id.desc())
        .limit(limit).all()
    )

    rent_created = sum(1 for r in rows if r.action == "RENT_CREATED")
    rent_total = sum(float(r.amount or 0) for r in rows if r.action == "RENT_CREATED")
    penalty_total = sum(float(r.amount or 0) for r in rows
                        if r.action == "PENALTY_APPLIED")

    return {
        "customer": {
            "id": user.id, "name": user.name, "mobile": user.mobile,
            "rent_bill_date": user.rent_bill_date,
            "auto_rent_bill_enabled": bool(user.auto_rent_bill_enabled),
            "is_active": bool(user.is_active),
        },
        "totals": {
            "rent_bills_created": rent_created,
            "rent_amount_total": round(rent_total, 2),
            "penalty_amount_total": round(penalty_total, 2),
        },
        "events": [_item_dict(r) for r in rows],
    }


@router.get("/api/scheduler/bills/{bill_id}", tags=["Scheduler"])
def bill_tracking(bill_id: int, db: Session = Depends(get_db),
                  _: User = Depends(require_admin)):
    """
    What the schedulers did to one bill, and why - the answer to "where did
    this penalty come from".

    The bill's current figures alongside the history that produced them, so
    the two can be read against each other rather than trusted separately.
    """
    bill = db.query(Bill).filter(Bill.id == bill_id).first()
    if not bill:
        raise HTTPException(404, detail="Bill not found")

    rows = (
        db.query(SchedulerRunItem)
        .filter(SchedulerRunItem.bill_id == bill_id)
        .order_by(SchedulerRunItem.run_date.asc(), SchedulerRunItem.id.asc())
        .all()
    )
    shop = db.query(Shop).filter(Shop.id == bill.shop_id).first()
    tenant = db.query(User).filter(User.id == bill.user_id).first()

    return {
        "bill": {
            "id": bill.id,
            "bill_type": bill.bill_type,
            "user_id": bill.user_id,
            "user_name": tenant.name if tenant else None,
            "shop_id": bill.shop_id,
            "shop_number": shop.shop_number if shop else None,
            "amount": _float(bill.amount),
            "penalty_amount": _float(bill.penalty_amount),
            "penalty_days": bill.penalty_days,
            "penalty_charged_through": bill.penalty_charged_through,
            "paid_amount": _float(bill.paid_amount),
            "pending_amount": _float(bill.pending_amount),
            "bill_date": bill.bill_date,
            "due_date": bill.due_date,
            "status": bill.status,
            "rent_period": bill.rent_period,
        },
        "history": [_item_dict(r) for r in rows],
    }


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

def _report(db: Session, group_expr, label_key: str, scheduler: Optional[str],
            date_from: Optional[date], date_to: Optional[date]) -> list:
    """
    One aggregation, three granularities.

    Rent and penalty totals are counted separately rather than added together:
    they are different kinds of money, and a single "amount" column that
    silently mixed rent raised with late fees charged would be worse than no
    total at all.

    Only items that actually happened are counted. A skipped duplicate or a
    failed row appears everywhere else in this API, but totalling it into
    "rent raised" would overstate the month.
    """
    query = db.query(
        group_expr.label("bucket"),
        SchedulerRunItem.action,
        func.count(SchedulerRunItem.id).label("items"),
        func.sum(func.coalesce(SchedulerRunItem.amount, 0)).label("amount"),
    ).filter(SchedulerRunItem.status == "SUCCESS")

    if scheduler:
        query = query.filter(SchedulerRunItem.scheduler == scheduler)
    query = _apply_date_range(query, SchedulerRunItem.run_date, date_from, date_to)

    rows = query.group_by("bucket", SchedulerRunItem.action).order_by("bucket").all()

    buckets = {}
    for row in rows:
        bucket = buckets.setdefault(str(row.bucket), {
            label_key: str(row.bucket),
            "rent_bills_created": 0, "rent_amount": 0.0,
            "penalties_applied": 0, "penalty_amount": 0.0,
        })
        if row.action == "RENT_CREATED":
            bucket["rent_bills_created"] += int(row.items or 0)
            bucket["rent_amount"] += float(row.amount or 0)
        elif row.action in ("PENALTY_APPLIED", "PENALTY_REDUCED"):
            # The COUNT is of penalties charged; a reduction is not one, so it
            # does not inflate the tally. The AMOUNT includes both, because a
            # reduction genuinely gives money back and a total that ignored it
            # would overstate what was actually charged that period.
            if row.action == "PENALTY_APPLIED":
                bucket["penalties_applied"] += int(row.items or 0)
            bucket["penalty_amount"] += float(row.amount or 0)

    for bucket in buckets.values():
        bucket["rent_amount"] = round(bucket["rent_amount"], 2)
        bucket["penalty_amount"] = round(bucket["penalty_amount"], 2)

    return sorted(buckets.values(), key=lambda b: b[label_key])


@router.get("/api/scheduler/reports/daily", tags=["Scheduler"])
def report_daily(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    scheduler: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
):
    """Rent raised and penalties applied, per day. Defaults to the last 30 days."""
    if not date_from and not date_to:
        date_to = date.today()
        date_from = date_to - timedelta(days=30)
    return {"granularity": "daily", "from": date_from, "to": date_to,
            "rows": _report(db, SchedulerRunItem.run_date, "date",
                            scheduler, date_from, date_to)}


@router.get("/api/scheduler/reports/monthly", tags=["Scheduler"])
def report_monthly(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    scheduler: Optional[str] = Query(None),
    year: Optional[int] = Query(None, description="defaults to the current year"),
):
    """Rent raised and penalties applied, per month of one year."""
    year = year or date.today().year
    bucket = func.substr(cast(SchedulerRunItem.run_date, String), 1, 7)
    return {"granularity": "monthly", "year": year,
            "rows": _report(db, bucket, "month", scheduler,
                            date(year, 1, 1), date(year, 12, 31))}


@router.get("/api/scheduler/reports/yearly", tags=["Scheduler"])
def report_yearly(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    scheduler: Optional[str] = Query(None),
):
    """Rent raised and penalties applied, per year, for as far back as records go."""
    bucket = func.substr(cast(SchedulerRunItem.run_date, String), 1, 4)
    return {"granularity": "yearly",
            "rows": _report(db, bucket, "year", scheduler, None, None)}


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS  (the only write path in this module)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/scheduler/settings", tags=["Scheduler"])
def get_scheduler_settings(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    The switches the two scripts obey, read from the same place they read them.

    They live in the database rather than in the scripts so the penalty rate
    can be changed here instead of by editing Python on the server - and so
    this screen shows the values the scripts will actually use tonight.
    """
    values = settings_service.get_all(db)
    schema = settings_service.describe_for("scheduler")
    return {
        "settings": [dict(item, value=values.get(item["key"])) for item in schema],
        "values": {item["key"]: values.get(item["key"]) for item in schema},
    }


@router.put("/api/scheduler/settings", tags=["Scheduler"])
def update_scheduler_settings(
    payload: SettingsUpdateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Change the scheduler switches. Admin only, and scheduler keys only.

    The key-prefix check is enforced here rather than merely hidden in the UI:
    this endpoint must not become a second way to write the application's own
    settings, whatever a crafted request asks for.
    """
    intruders = sorted(k for k in payload.values if not settings_service.is_scheduler_key(k))
    if intruders:
        raise HTTPException(
            400,
            detail=f"These are not scheduler settings: {', '.join(intruders)}. "
                   f"Use the main Settings screen for those.",
        )

    try:
        changed = settings_service.set_many(db, payload.values, actor_id=actor.id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))

    write_audit(db, actor.id, "UPDATE", "app_settings", None, new_data=changed)
    db.commit()
    settings_service.invalidate_cache()

    return {"message": "Scheduler settings updated", "changed": changed}
