"""
scheduler/audit.py - the scheduler's write path into audit_logs.

Same table and same row shape the application writes, so a bill created by the
nightly run and one created by an admin appear side by side in the audit trail
rather than the automatic ones being invisible.

`user_id` is NULL for everything written here: no human took this action. That
is what distinguishes an automatic run from an admin's click when reading the
trail back.
"""

import json
from typing import Optional

from sqlalchemy.orm import Session

from scheduler.models import AuditLog


def write_audit(
    db: Session,
    action: str,
    table_name: str,
    record_id: Optional[int] = None,
    old_data: Optional[dict] = None,
    new_data: Optional[dict] = None,
) -> None:
    """Queue one audit row. The caller commits."""
    db.add(AuditLog(
        user_id    = None,
        action     = action,
        table_name = table_name,
        record_id  = record_id,
        old_data   = json.dumps(old_data, default=str) if old_data else None,
        new_data   = json.dumps(new_data, default=str) if new_data else None,
    ))
