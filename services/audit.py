"""
services/audit.py - the single write path for audit_logs rows.

Extracted verbatim from app.py alongside core/security.py (step 2 of the
router/service split). Kept separate from core/security.py because it's a
distinct concern (write-side of audit logging vs. authentication) that many
non-auth routers also need directly.
"""

import json
from typing import Optional

from sqlalchemy.orm import Session

from models.schema import AuditLog


def write_audit(
    db: Session,
    actor_id: int,
    action: str,
    table_name: str,
    record_id: Optional[int] = None,
    old_data: Optional[dict] = None,
    new_data: Optional[dict] = None,
):
    """Persist one audit log entry."""
    entry = AuditLog(
        user_id    = actor_id,
        action     = action,
        table_name = table_name,
        record_id  = record_id,
        old_data   = json.dumps(old_data,  default=str) if old_data  else None,
        new_data   = json.dumps(new_data,  default=str) if new_data  else None,
    )
    db.add(entry)
    # caller commits
