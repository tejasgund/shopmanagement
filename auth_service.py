"""
auth_service.py - JWT issuing/validation and the FastAPI auth dependencies
(get_current_user / require_admin / require_tenant) used by every protected
route in this app.

Extracted verbatim from app.py (step 2 of the router/service split - see
schemas.py for step 1). This exists as its own module, separate from app.py,
specifically so that future router modules can depend on require_admin /
require_tenant without importing app.py itself (which would be a circular
import, since app.py is what wires the routers together).

Nothing about behavior changed in this move: same JWT_SECRET/ALGORITHM/
EXPIRE_MINUTES resolution order (env var, falling back to the same
hardcoded defaults), same token payload/expiry handling, same 401/403 rules.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from db_config import get_db
from create_tables import User

# ──────────────────────────────────────────────
# JWT Settings
# ──────────────────────────────────────────────
JWT_SECRET    = os.getenv("JWT_SECRET",    "CHANGE_ME_IN_PRODUCTION_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 24 hours

security = HTTPBearer()


# ══════════════════════════════════════════════════════════════════════════════
# JWT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def create_access_token(data: dict) -> str:
    """Encode a JWT token that expires after JWT_EXPIRE_MINUTES minutes."""
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Raises HTTPException on failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


# ══════════════════════════════════════════════════════════════════════════════
# AUTH DEPENDENCIES
# ══════════════════════════════════════════════════════════════════════════════

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Dependency – returns the authenticated User ORM object."""
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")

    if user_id is None:
        raise HTTPException(status_code=401, detail="Token missing subject")

    user = db.query(User).filter(User.id == int(user_id), User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency – raises 403 unless the caller is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def require_tenant(current_user: User = Depends(get_current_user)) -> User:
    """Dependency – any authenticated user may pass (admin or tenant)."""
    return current_user
