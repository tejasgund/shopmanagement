"""
routers/audit_log.py - GET /api/audit-logs* (Admin only).

Extracted verbatim from app.py (step 3 of the router/service split, after
schemas.py and auth_service.py/audit_service.py). Self-contained: only
touches the AuditLog/User models directly and the shared require_admin
dependency, so it was the first full route module safe to pull out.
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import AuditLog, User
from auth_service import require_admin

router = APIRouter(tags=["Audit Log"])


def _parse_json_field(value):
    """Safely parse a JSON string stored in the DB; return dict/list or None."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value  # return raw if unparseable


def _audit_log_to_dict(log: "AuditLog", user: Optional["User"]) -> dict:
    return {
        "id":         log.id,
        "created_at": log.created_at,
        "action":     log.action,
        "table_name": log.table_name,
        "record_id":  log.record_id,
        "user": {
            "id":     user.id     if user else log.user_id,
            "name":   user.name   if user else "Unknown",
            "mobile": user.mobile if user else "",
            "role":   user.role   if user else "",
        },
        "old_data": _parse_json_field(log.old_data),
        "new_data": _parse_json_field(log.new_data),
    }


@router.get("/api/audit-logs/filters", tags=["Audit Log"])
def audit_log_filters(
    db:    Session = Depends(get_db),
    _:     User    = Depends(require_admin),
):
    """
    Return distinct values for action and table_name that exist in the audit log.
    Use this to populate filter dropdowns in the UI.  Admin only.

    Response:
        {
          "actions":      ["CREATE", "UPDATE", "DELETE", ...],
          "table_names":  ["bills", "payments", "users", ...]
        }
    """
    actions     = [r[0] for r in db.query(AuditLog.action    ).distinct().order_by(AuditLog.action).all()     if r[0]]
    table_names = [r[0] for r in db.query(AuditLog.table_name).distinct().order_by(AuditLog.table_name).all() if r[0]]
    return {"success": True, "actions": actions, "table_names": table_names}


@router.get("/api/audit-logs", tags=["Audit Log"])
def list_audit_logs(
    # ── Pagination ────────────────────────────────
    page:       int            = Query(1,  ge=1),
    limit:      int            = Query(20, ge=1, le=200),
    # ── Filters ───────────────────────────────────
    user_id:    Optional[int]  = None,
    action:     Optional[str]  = None,
    table_name: Optional[str]  = None,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
    # ── Search ────────────────────────────────────
    search:     Optional[str]  = Query(None, description="Search by user name, mobile, action, table_name, or record_id"),
    # ── DB / Auth ─────────────────────────────────
    db:         Session        = Depends(get_db),
    _:          User           = Depends(require_admin),
):
    """
    Paginated, filterable, searchable list of all audit log entries.
    Results are sorted newest-first by default.  Admin only.

    Query params:
        page        – page number (default 1)
        limit       – page size   (default 20, max 200)
        user_id     – filter by acting user
        action      – exact match on action  (CREATE / UPDATE / DELETE / LOGIN …)
        table_name  – exact match on table   (bills / payments / users …)
        start_date  – ISO-8601, inclusive lower bound on created_at
        end_date    – ISO-8601, inclusive upper bound on created_at
        search      – free-text search across user name, mobile, action,
                      table_name, and record_id
    """
    # Build base query joining users so we can search / return actor info
    q = (
        db.query(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
    )

    # ── Exact filters ─────────────────────────────
    if user_id is not None:
        q = q.filter(AuditLog.user_id == user_id)
    if action:
        q = q.filter(AuditLog.action == action.upper())
    if table_name:
        q = q.filter(AuditLog.table_name == table_name.lower())
    if start_date:
        q = q.filter(AuditLog.created_at >= start_date)
    if end_date:
        # include the whole end day even if no time component given
        end_inclusive = end_date.replace(hour=23, minute=59, second=59) if end_date.hour == 0 else end_date
        q = q.filter(AuditLog.created_at <= end_inclusive)

    # ── Free-text search ──────────────────────────
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            User.name.ilike(term)
            | User.mobile.ilike(term)
            | AuditLog.action.ilike(term)
            | AuditLog.table_name.ilike(term)
            | AuditLog.record_id.cast(text("CHAR")).ilike(term)
        )

    # ── Count before pagination ───────────────────
    total = q.count()

    # ── Sort + paginate ───────────────────────────
    offset = (page - 1) * limit
    rows   = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(limit).all()

    return {
        "success": True,
        "page":    page,
        "limit":   limit,
        "total":   total,
        "data":    [_audit_log_to_dict(log, user) for log, user in rows],
    }


@router.get("/api/audit-logs/{log_id}", tags=["Audit Log"])
def get_audit_log(
    log_id: int,
    db:     Session = Depends(get_db),
    _:      User    = Depends(require_admin),
):
    """
    Return the full detail of a single audit log entry, including parsed
    old_data / new_data JSON and full actor information.  Admin only.
    """
    row = (
        db.query(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .filter(AuditLog.id == log_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Audit log entry not found")
    log, user = row
    return {"success": True, "data": _audit_log_to_dict(log, user)}
