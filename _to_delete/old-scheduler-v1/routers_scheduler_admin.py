"""
routers/scheduler_admin.py - the Scheduler Monitoring dashboard API (admin only).

Every list here is read straight from the scheduler_tasks table. There is no
hardcoded catalogue of tasks in this file and none in the frontend: the task
names, labels and descriptions come from scheduler.service.TASKS, so a task
added to that registry appears on the dashboard on its own.

Read-only apart from three deliberate actions: retrying a finished task,
running a sweep on demand, and editing the Scheduler app's own settings.

Those settings live here rather than on the main /api/settings screen. They
share the same storage, validation and audit trail as every other setting -
there is no second settings mechanism - but they belong to this app, and the
main admin Settings endpoint refuses to read or write them. Enforced on both
sides so the boundary holds regardless of which UI is calling.
"""


from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import get_db
from core.security import require_admin
from models.schema import SchedulerTask, User
from schemas.api import SettingsUpdateRequest
from services import settings as settings_service
from services.audit import write_audit
# The dependency runs one way only: this app imports the scheduler, and the
# scheduler imports nothing from here. That is what keeps scheduler/ a
# separately deployable folder while these routes stay in the main app.
from scheduler import master as scheduler_master
from scheduler import service as svc
from scheduler.billing import penalty

router = APIRouter(tags=["Scheduler"])


@router.get("/api/scheduler/status", tags=["Scheduler"])
def scheduler_status(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    The headline: is it on, when did it last run, what is next, what is broken.

    `next_expected_run` is the earliest FUTURE occurrence rather than the cron
    expression - the ledger knows what is actually scheduled, whereas the
    crontab lives on a machine this process cannot read.
    """
    cfg = settings_service.get_all(db)
    now = svc.now_local()

    last_run = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.finished_at.isnot(None))
        .order_by(SchedulerTask.finished_at.desc())
        .first()
    )
    last_success = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.status == svc.COMPLETED)
        .order_by(SchedulerTask.finished_at.desc())
        .first()
    )
    next_run = (
        db.query(SchedulerTask)
        .filter(SchedulerTask.status == svc.PENDING, SchedulerTask.scheduled_for > now)
        .order_by(SchedulerTask.scheduled_for.asc())
        .first()
    )

    counts = {
        status: db.query(func.count(SchedulerTask.id))
                  .filter(SchedulerTask.status == status).scalar() or 0
        for status in (svc.PENDING, svc.RUNNING, svc.COMPLETED, svc.FAILED, svc.SKIPPED)
    }

    overdue = svc.due_tasks(db, now)
    missed = [r for r in overdue if svc.is_missed(r, now)]
    stuck = [
        r for r in db.query(SchedulerTask).filter(SchedulerTask.status == svc.RUNNING).all()
        if svc.is_stuck(r, now)
    ]

    return {
        "server_time": now,
        "scheduler_enabled": svc.scheduler_enabled(cfg),
        "tasks": [
            {
                "task_name": name,
                "label": spec["label"],
                "description": spec["description"],
                "enable_setting": spec.get("enable_setting"),
                "enabled": svc.task_enabled(cfg, name),
                "run_at": spec["run_at"].strftime("%H:%M"),
            }
            for name, spec in sorted(svc.TASKS.items(), key=lambda kv: kv[1]["run_at"])
        ],
        "last_run": svc.to_dict(last_run, now) if last_run else None,
        "last_successful_run": svc.to_dict(last_success, now) if last_success else None,
        "next_expected_run": svc.to_dict(next_run, now) if next_run else None,
        "counts": counts,
        "failed_count": counts[svc.FAILED],
        "missed_count": len(missed),
        "stuck_count": len(stuck),
        "penalty": penalty.penalty_settings(cfg),
    }


def _listing(db, statuses=None, only_missed=False, only_future=False,
             task_name=None, limit=50, order_desc=True):
    now = svc.now_local()
    q = db.query(SchedulerTask)
    if statuses:
        q = q.filter(SchedulerTask.status.in_(statuses))
    if task_name:
        q = q.filter(SchedulerTask.task_name == task_name)
    if only_future:
        q = q.filter(SchedulerTask.scheduled_for > now)
    if only_missed:
        q = q.filter(SchedulerTask.scheduled_for < now - svc.MISSED_AFTER)
    q = q.order_by(
        SchedulerTask.scheduled_for.desc() if order_desc else SchedulerTask.scheduled_for.asc()
    )
    return [svc.to_dict(r, now) for r in q.limit(limit).all()]


@router.get("/api/scheduler/tasks", tags=["Scheduler"])
def scheduler_tasks(
    view:      str = Query("history",
                           pattern="^(running|upcoming|completed|failed|missed|skipped|history)$"),
    task_name: Optional[str] = None,
    limit:     int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _:  User    = Depends(require_admin),
):
    """
    One endpoint, several named views - so the dashboard adds a tab without
    the API growing an endpoint each time.

    "missed" is the important one: PENDING and already past its time. Those
    rows are the whole point of writing expected runs down in advance.
    """
    if view == "running":
        return _listing(db, statuses=[svc.RUNNING], task_name=task_name, limit=limit)
    if view == "upcoming":
        return _listing(db, statuses=[svc.PENDING], only_future=True,
                        task_name=task_name, limit=limit, order_desc=False)
    if view == "completed":
        return _listing(db, statuses=[svc.COMPLETED], task_name=task_name, limit=limit)
    if view == "failed":
        return _listing(db, statuses=[svc.FAILED], task_name=task_name, limit=limit)
    if view == "skipped":
        return _listing(db, statuses=[svc.SKIPPED], task_name=task_name, limit=limit)
    if view == "missed":
        return _listing(db, statuses=[svc.PENDING], only_missed=True,
                        task_name=task_name, limit=limit, order_desc=False)
    return _listing(db, task_name=task_name, limit=limit)


@router.get("/api/scheduler/tasks/{task_id}", tags=["Scheduler"])
def scheduler_task_detail(task_id: int, db: Session = Depends(get_db),
                          _: User = Depends(require_admin)):
    row = db.query(SchedulerTask).filter(SchedulerTask.id == task_id).first()
    if not row:
        raise HTTPException(404, detail="Scheduler task not found")
    return svc.to_dict(row)


@router.post("/api/scheduler/tasks/{task_id}/retry", tags=["Scheduler"])
def scheduler_task_retry(task_id: int, db: Session = Depends(get_db),
                         actor: User = Depends(require_admin)):
    """
    Put a finished run back in the queue.

    Only FAILED and SKIPPED rows can be retried. A COMPLETED one is refused on
    purpose: re-running it would be harmless (every task is idempotent) but it
    would overwrite a good record of what happened, and losing history to a
    misclick is worse than the inconvenience.
    """
    row = db.query(SchedulerTask).filter(SchedulerTask.id == task_id).first()
    if not row:
        raise HTTPException(404, detail="Scheduler task not found")
    if row.status not in (svc.FAILED, svc.SKIPPED):
        raise HTTPException(
            400,
            detail=f"Only failed or skipped tasks can be retried; this one is {row.status}.",
        )

    svc.retry(db, row)
    write_audit(db, actor.id, "RETRY", "scheduler_tasks", row.id,
                new_data={"task_name": row.task_name, "scheduled_for": row.scheduled_for})
    db.commit()
    return {"success": True, "message": f"{row.task_name} queued for retry.",
            "task": svc.to_dict(row)}


@router.post("/api/scheduler/run", tags=["Scheduler"])
def scheduler_run_now(
    task_name: Optional[str] = Query(None, description="Restrict the sweep to one task."),
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Run a sweep on demand, without waiting for cron.

    The same coordinator cron calls, so this cannot behave differently from the
    nightly run - useful for clearing a backlog immediately after fixing
    whatever broke.
    """
    summary = scheduler_master.run_due_tasks(db, only_task=task_name)
    write_audit(db, actor.id, "RUN", "scheduler_tasks", None,
                new_data={"tasks_run": summary["tasks_run"],
                          "tasks_failed": summary["tasks_failed"],
                          "tasks_skipped": summary["tasks_skipped"]})
    db.commit()
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER SETTINGS  (this app's own; the main Settings screen cannot see them)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/scheduler/settings", tags=["Scheduler"])
def get_scheduler_settings(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    The Scheduler app's settings, in the same shape as /api/settings so the
    page can render them from the schema rather than hardcoding each field.
    """
    values = settings_service.get_all(db)
    schema = settings_service.describe_for("scheduler")
    for item in schema:
        item["value"] = values.get(item["key"], item["default"])
    return {
        "categories": sorted({item["category"] for item in schema}),
        "settings": schema,
    }


@router.put("/api/scheduler/settings", tags=["Scheduler"])
def update_scheduler_settings(
    payload: SettingsUpdateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """
    Update the Scheduler app's settings.

    The mirror image of the guard on /api/settings: this endpoint accepts ONLY
    scheduler keys, so the Scheduler page cannot reach across and change the
    main application's branding or billing configuration either. The boundary
    is refused in both directions rather than trusted to the UI.
    """
    if not payload.values:
        raise HTTPException(400, detail="No settings were provided")

    intruders = sorted(k for k in payload.values if not settings_service.is_scheduler_key(k))
    if intruders:
        raise HTTPException(
            400,
            detail="Only scheduler settings can be changed here: " + ", ".join(intruders),
        )

    try:
        changed = settings_service.set_many(db, payload.values, actor_id=actor.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, detail=str(exc))

    if changed:
        write_audit(db, actor.id, "UPDATE", "app_settings", None, new_data=changed)
    db.commit()
    settings_service.invalidate_cache()

    return {
        "success": True,
        "message": f"{len(changed)} setting(s) updated" if changed else "No changes to save",
        "changed": changed,
    }
