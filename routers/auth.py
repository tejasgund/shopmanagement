"""
routers/auth.py - POST /api/login.

Extracted verbatim from app.py (step 21 of the router/service split). Uses
create_access_token from core/security.py (step 2) and write_audit from
services/audit.py (step 2) - same JWT payload, same audit trail, same
401/403 rules as before.
"""

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.database import get_db
from core.logger import get_logger
from core.security import create_access_token
from models.schema import User
from schemas.api import LoginRequest, LoginResponse
from services.audit import write_audit

logger = get_logger("app")

router = APIRouter(tags=["Authentication"])


@router.post("/api/login", response_model=LoginResponse, tags=["Authentication"])
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with mobile + password and receive a JWT token."""
    user = db.query(User).filter(User.mobile == payload.mobile).first()

    if not user or not bcrypt.checkpw(payload.password.encode(), user.password_hash.encode()):
        raise HTTPException(status_code=401, detail="Invalid mobile or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    token = create_access_token({"sub": str(user.id), "role": user.role})

    write_audit(db, user.id, "LOGIN", "users", user.id)
    db.commit()

    logger.info("LOGIN | user_id=%s | mobile=%s | role=%s", user.id, user.mobile, user.role)
    return LoginResponse(success=True, token=token, role=user.role)
