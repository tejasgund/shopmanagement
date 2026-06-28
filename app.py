"""
app.py - Main FastAPI Application
Tenant Management System

Features:
    - JWT Authentication (HS256)
    - Role-Based Access Control (admin / tenant)
    - Full CRUD for complexes, shops, users
    - Bill management with auto payment reconciliation
    - Tenant read-only portal
    - Audit logging on every mutating operation
    - Swagger / ReDoc documentation at /docs and /redoc
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

from db_config import get_db
from log import get_logger, log_request_middleware
from fastapi.middleware.cors import CORSMiddleware


# Import ORM models from create_tables so we have a single schema source-of-truth
from create_tables import (
    AuditLog, Bill, Complex, DepositPayment, Payment, Shop, User, UserShop,
)

# ──────────────────────────────────────────────
# Logger
# ──────────────────────────────────────────────
logger = get_logger("app")

# ══════════════════════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════════════════════
app = FastAPI(
    title="Tenant Management System",
    description="REST API for managing tenants, shops, complexes, bills, and payments.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ──────────────────────────────────────────────
# CORS Configuration - Allow frontend access
# ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development - allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, PUT, DELETE, OPTIONS)
    allow_headers=["*"],  # Allows all headers
)

# Register request-logging middleware
app.middleware("http")(log_request_middleware)

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


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT HELPER
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

# ── Auth ──────────────────────────────────────
class LoginRequest(BaseModel):
    mobile:   str = Field(..., example="9999999999")
    password: str = Field(..., example="admin@123")


class LoginResponse(BaseModel):
    success: bool
    token:   str
    role:    str


# ── Complex ───────────────────────────────────
class ComplexCreate(BaseModel):
    name:        str  = Field(..., min_length=1, max_length=150)
    address:     Optional[str] = None
    description: Optional[str] = None


class ComplexUpdate(BaseModel):
    name:        Optional[str] = Field(None, min_length=1, max_length=150)
    address:     Optional[str] = None
    description: Optional[str] = None


class ComplexResponse(BaseModel):
    id:          int
    name:        str
    address:     Optional[str]
    description: Optional[str]
    created_at:  datetime
    updated_at:  datetime

    class Config:
        from_attributes = True


# ── Shop ──────────────────────────────────────
class ShopCreate(BaseModel):
    shop_number:  str             = Field(..., min_length=1, max_length=50)
    area_sqft:    Optional[float] = None
    status:       Optional[str]   = Field("available", pattern="^(available|occupied|maintenance)$")
    complex_id:   Optional[int]   = None
    shop_rent:    Optional[float] = Field(0, ge=0)
    shop_deposit: Optional[float] = Field(0, ge=0)


class ShopUpdate(BaseModel):
    shop_number:  Optional[str]   = Field(None, min_length=1, max_length=50)
    area_sqft:    Optional[float] = None
    status:       Optional[str]   = Field(None, pattern="^(available|occupied|maintenance)$")
    complex_id:   Optional[int]   = None
    shop_rent:    Optional[float] = Field(None, ge=0)
    shop_deposit: Optional[float] = Field(None, ge=0)


class ShopOwnerInfo(BaseModel):
    id:     int
    name:   str
    mobile: str


class ShopResponse(BaseModel):
    id:           int
    shop_number:  str
    area_sqft:    Optional[float]
    status:       str
    complex_id:   Optional[int]
    shop_rent:    float
    shop_deposit: float
    created_at:   datetime
    updated_at:   datetime
    assigned_to:  Optional[ShopOwnerInfo] = None

    class Config:
        from_attributes = True


class AssignComplexRequest(BaseModel):
    complex_id: int


# ── User ──────────────────────────────────────
class UserCreate(BaseModel):
    name:     str            = Field(..., min_length=1, max_length=120)
    mobile:   str            = Field(..., min_length=10, max_length=15)
    email:    Optional[str]  = None
    password: str            = Field(..., min_length=6)
    role:     Optional[str]  = Field("tenant", pattern="^(admin|tenant)$")


class UserUpdate(BaseModel):
    name:     Optional[str] = Field(None, min_length=1, max_length=120)
    mobile:   Optional[str] = Field(None, min_length=10, max_length=15)
    email:    Optional[str] = None
    password: Optional[str] = Field(None, min_length=6)
    role:     Optional[str] = Field(None, pattern="^(admin|tenant)$")
    is_active:Optional[bool]= None


class UserResponse(BaseModel):
    id:        int
    name:      str
    mobile:    str
    email:     Optional[str]
    role:      str
    is_active: bool
    created_at:datetime
    updated_at:datetime

    class Config:
        from_attributes = True


class AssignShopsRequest(BaseModel):
    shop_ids:    List[int]               = Field(..., min_length=1)
    force:       bool                    = Field(False, description="If true, reassign shops already owned by another active tenant.")
    agreed_rent: Optional[float]         = Field(None, ge=0, description="Rent agreed with this tenant. If omitted, defaults to each shop's shop_rent.")
    shop_rents:  Optional[dict[int, float]] = Field(None, description="Optional per-shop agreed_rent override, keyed by shop_id. Takes precedence over agreed_rent.")


class DetachShopsRequest(BaseModel):
    shop_ids: List[int] = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=4)


# ── Bill ──────────────────────────────────────
class BillCreate(BaseModel):
    user_id:     int
    shop_id:     int
    bill_type:   str               = Field(..., min_length=1, max_length=80,
                                            description='Use "Rent" to auto-fill amount from the agreed rent for this tenant/shop, or any other value (e.g. "Electricity", "Maintenance", "Other") for manual entry.')
    amount:      Optional[float]   = Field(None, gt=0,
                                            description="Required when bill_type is not Rent. Ignored (recomputed) when bill_type is Rent.")
    description: Optional[str]     = None
    due_date:    Optional[datetime] = None


class BillResponse(BaseModel):
    id:             int
    user_id:        int
    shop_id:        int
    bill_type:      str
    description:    Optional[str]
    amount:         float
    paid_amount:    float
    pending_amount: float
    bill_date:      datetime
    due_date:       Optional[datetime]
    status:         str
    created_at:     datetime

    class Config:
        from_attributes = True


# ── Payment ───────────────────────────────────
class PaymentCreate(BaseModel):
    bill_id:        int
    amount:         float  = Field(..., gt=0)
    payment_method: str    = Field(..., min_length=1, max_length=60)
    remarks:        Optional[str] = None


class PaymentResponse(BaseModel):
    id:             int
    bill_id:        int
    amount:         float
    payment_method: str
    payment_date:   datetime
    remarks:        Optional[str]
    created_at:     datetime

    class Config:
        from_attributes = True


# ── Deposit Payment ───────────────────────────
class DepositPaymentCreate(BaseModel):
    user_id:      int
    shop_id:      int
    amount:       float           = Field(..., gt=0)
    payment_date: Optional[datetime] = None
    remarks:      Optional[str]   = None


class DepositPaymentResponse(BaseModel):
    id:           int
    user_id:      int
    shop_id:      int
    shop_number:  str
    amount:       float
    payment_date: datetime
    remarks:      Optional[str]
    created_at:   datetime

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════════════════

def _decimal_to_float(value) -> float:
    """Convert Decimal to float for Pydantic serialisation."""
    return float(value) if isinstance(value, Decimal) else (value or 0.0)


def _shop_owner_map(db: Session, shop_ids: Optional[List[int]] = None) -> dict:
    """
    Build {shop_id: ShopOwnerInfo} for current owners.
    Since one shop should have at most one active owner, this takes the
    most recently assigned UserShop row per shop as the "current" owner.
    """
    q = db.query(UserShop, User).join(User, User.id == UserShop.user_id)
    if shop_ids is not None:
        q = q.filter(UserShop.shop_id.in_(shop_ids))
    rows = q.order_by(UserShop.shop_id, UserShop.assigned_at.desc()).all()

    owner_map = {}
    for user_shop, user in rows:
        if user_shop.shop_id not in owner_map:  # first row per shop = most recent
            owner_map[user_shop.shop_id] = ShopOwnerInfo(id=user.id, name=user.name, mobile=user.mobile)
    return owner_map


def _shop_to_response(shop: Shop, owner_map: dict) -> ShopResponse:
    data = ShopResponse.model_validate(shop)
    data.assigned_to = owner_map.get(shop.id)
    return data


def _reconcile_bill(bill: Bill):
    """Recompute paid_amount, pending_amount, and status from linked payments."""
    total_paid     = sum(_decimal_to_float(p.amount) for p in bill.payments)
    bill_amount    = _decimal_to_float(bill.amount)
    bill.paid_amount    = Decimal(str(total_paid))
    bill.pending_amount = Decimal(str(max(0.0, bill_amount - total_paid)))

    if total_paid <= 0:
        bill.status = "pending"
    elif total_paid >= bill_amount:
        bill.status = "paid"
    else:
        bill.status = "partial"


def _current_user_shops(db: Session, user_id: int) -> List[UserShop]:
    """All current UserShop assignment rows for a given user."""
    return db.query(UserShop).filter(UserShop.user_id == user_id).all()


def _agreed_rent_for(user_shop: UserShop, shop: Shop) -> float:
    """The effective monthly rent for a tenant/shop: agreed_rent override if
    set, otherwise falls back to the shop's standard shop_rent."""
    if user_shop.agreed_rent is not None:
        return _decimal_to_float(user_shop.agreed_rent)
    return _decimal_to_float(shop.shop_rent)


def _deposit_paid_for_shop(db: Session, user_id: int, shop_id: int) -> float:
    rows = db.query(DepositPayment).filter(
        DepositPayment.user_id == user_id, DepositPayment.shop_id == shop_id
    ).all()
    return sum(_decimal_to_float(r.amount) for r in rows)


def _pending_rent_for_user(db: Session, user_id: int) -> float:
    """Sum of pending_amount across all non-paid Rent bills for a user."""
    rows = (
        db.query(Bill)
        .filter(Bill.user_id == user_id, Bill.bill_type == "Rent", Bill.status != "paid")
        .all()
    )
    return sum(_decimal_to_float(b.pending_amount) for b in rows)


def _build_user_financial_summary(db: Session, user: User) -> dict:
    """
    Shared logic for /api/user/{id}/financial-summary and
    /api/tenant/financial-summary (self), since both need the same shape.
    """
    user_shops = _current_user_shops(db, user.id)
    shop_ids = [us.shop_id for us in user_shops]
    shops = {s.id: s for s in db.query(Shop).filter(Shop.id.in_(shop_ids)).all()} if shop_ids else {}
    complexes = {c.id: c for c in db.query(Complex).all()}

    shops_summary_list = []
    total_monthly_rent = 0.0
    total_deposit_required = 0.0
    total_deposit_paid = 0.0

    for us in user_shops:
        shop = shops.get(us.shop_id)
        if not shop:
            continue
        rent = _agreed_rent_for(us, shop)
        deposit_required = _decimal_to_float(shop.shop_deposit)
        deposit_paid = _deposit_paid_for_shop(db, user.id, shop.id)

        total_monthly_rent += rent
        total_deposit_required += deposit_required
        total_deposit_paid += deposit_paid

        shops_summary_list.append({
            "id": shop.id,
            "shop_number": shop.shop_number,
            "complex_id": shop.complex_id,
            "complex_name": complexes.get(shop.complex_id).name if shop.complex_id and complexes.get(shop.complex_id) else None,
            "area_sqft": _decimal_to_float(shop.area_sqft),
            "shop_rent": rent,
            "shop_deposit": deposit_required,
            "status": shop.status,
        })

    total_pending_rent = _pending_rent_for_user(db, user.id)
    total_rent_collected_or_paid = sum(
        _decimal_to_float(b.paid_amount)
        for b in db.query(Bill).filter(Bill.user_id == user.id, Bill.bill_type == "Rent").all()
    )

    bills = db.query(Bill).filter(Bill.user_id == user.id).order_by(Bill.id).all()
    shop_numbers = {s.id: s.shop_number for s in shops.values()}

    bills_list = [
        {
            "id": b.id, "shop_id": b.shop_id, "shop_number": shop_numbers.get(b.shop_id),
            "bill_type": b.bill_type, "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount), "pending_amount": _decimal_to_float(b.pending_amount),
            "status": b.status, "bill_date": b.bill_date, "due_date": b.due_date,
            "description": b.description,
        }
        for b in bills
    ]

    payments = (
        db.query(Payment).join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == user.id).order_by(Payment.id).all()
    )
    payment_history = [
        {
            "id": p.id, "bill_id": p.bill_id, "shop_id": p.bill.shop_id,
            "shop_number": shop_numbers.get(p.bill.shop_id), "bill_type": p.bill.bill_type,
            "amount": _decimal_to_float(p.amount), "payment_method": p.payment_method,
            "payment_date": p.payment_date, "remarks": p.remarks,
        }
        for p in payments
    ]

    deposit_payments = (
        db.query(DepositPayment).filter(DepositPayment.user_id == user.id).order_by(DepositPayment.id).all()
    )
    deposit_payment_history = [
        {
            "id": dp.id, "shop_id": dp.shop_id, "shop_number": shop_numbers.get(dp.shop_id),
            "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
            "remarks": dp.remarks,
        }
        for dp in deposit_payments
    ]

    return {
        "user": {
            "id": user.id, "name": user.name, "mobile": user.mobile,
            "email": user.email, "role": user.role, "is_active": user.is_active,
        },
        "shops_summary": {"total_shops": len(shops_summary_list), "shops": shops_summary_list},
        "shops": shops_summary_list,  # convenience alias for tenant self-summary consumers
        "rent_summary": {
            "total_monthly_rent": round(total_monthly_rent, 2),
            "total_pending_rent": round(total_pending_rent, 2),
            "total_rent_collected": round(total_rent_collected_or_paid, 2),
            "total_rent_paid": round(total_rent_collected_or_paid, 2),
        },
        "deposit_summary": {
            "total_deposit_required": round(total_deposit_required, 2),
            "total_deposit_paid": round(total_deposit_paid, 2),
            "remaining_deposit": round(total_deposit_required - total_deposit_paid, 2),
        },
        "outstanding_balance": round(total_pending_rent, 2),
        "bills": bills_list,
        "payment_history": payment_history,
        "deposit_payment_history": deposit_payment_history,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: /api/login
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/login", response_model=LoginResponse, tags=["Authentication"])
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


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Complex Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/complex", response_model=ComplexResponse, status_code=201, tags=["Complex"])
def create_complex(
    body: ComplexCreate,
    db:   Session = Depends(get_db),
    actor: User   = Depends(require_admin),
):
    """Create a new complex. Admin only."""
    obj = Complex(**body.model_dump())
    db.add(obj)
    db.flush()
    write_audit(db, actor.id, "CREATE", "complexes", obj.id, new_data=body.model_dump())
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/api/complex", response_model=List[ComplexResponse], tags=["Complex"])
def list_complexes(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all complexes. Admin only."""
    return db.query(Complex).order_by(Complex.id).all()


# NOTE: this concrete path MUST be registered before "/api/complex/{id}" so
# FastAPI doesn't try to parse "summary" as an integer id.
@app.get("/api/complex/summary", tags=["Complex"])
def all_complexes_summary(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Summary statistics for ALL complexes. Used on the main Admin Dashboard. Admin only."""
    complexes = db.query(Complex).order_by(Complex.id).all()
    results = []
    for c in complexes:
        shops = db.query(Shop).filter(Shop.complex_id == c.id).all()
        total_shops = len(shops)
        occupied = [s for s in shops if s.status == "occupied"]
        available_count = sum(1 for s in shops if s.status == "available")
        total_monthly_rent = 0.0
        for s in occupied:
            us = db.query(UserShop).filter(UserShop.shop_id == s.id).order_by(UserShop.assigned_at.desc()).first()
            total_monthly_rent += _agreed_rent_for(us, s) if us else _decimal_to_float(s.shop_rent)
        total_pending_rent = (
            db.query(Bill)
            .join(Shop, Shop.id == Bill.shop_id)
            .filter(Shop.complex_id == c.id, Bill.bill_type == "Rent", Bill.status != "paid")
            .all()
        )
        results.append({
            "complex_id": c.id,
            "complex_name": c.name,
            "total_shops": total_shops,
            "occupied_shops": len(occupied),
            "available_shops": available_count,
            "total_monthly_rent": round(total_monthly_rent, 2),
            "total_pending_rent": round(sum(_decimal_to_float(b.pending_amount) for b in total_pending_rent), 2),
        })
    return results


@app.get("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def get_complex(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")
    return obj


@app.get("/api/complex/{id}/summary", tags=["Complex"])
def complex_summary(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Live statistics for a single complex. Used in the Complex Dashboard cards. Admin only."""
    c = db.query(Complex).filter(Complex.id == id).first()
    if not c:
        raise HTTPException(404, detail="Complex not found")

    shops = db.query(Shop).filter(Shop.complex_id == id).all()
    shop_ids = [s.id for s in shops]
    occupied = [s for s in shops if s.status == "occupied"]
    available_count = sum(1 for s in shops if s.status == "available")

    owner_map = _shop_owner_map(db, shop_ids) if shop_ids else {}

    total_monthly_rent = 0.0
    total_deposit_required = 0.0
    tenants_by_user = {}

    for s in shops:
        us = db.query(UserShop).filter(UserShop.shop_id == s.id).order_by(UserShop.assigned_at.desc()).first()
        deposit_required = _decimal_to_float(s.shop_deposit)
        total_deposit_required += deposit_required if s.status == "occupied" else 0.0
        if s.status != "occupied" or not us:
            continue
        rent = _agreed_rent_for(us, s)
        total_monthly_rent += rent

        owner = owner_map.get(s.id)
        if not owner:
            continue
        entry = tenants_by_user.setdefault(owner.id, {
            "user_id": owner.id, "user_name": owner.name, "mobile": owner.mobile,
            "shops": [], "monthly_rent": 0.0, "pending_rent": 0.0,
            "deposit_required": 0.0, "deposit_paid": 0.0,
        })
        entry["shops"].append(s.shop_number)
        entry["monthly_rent"] += rent
        entry["deposit_required"] += deposit_required
        entry["deposit_paid"] += _deposit_paid_for_shop(db, owner.id, s.id)
        entry["pending_rent"] = _pending_rent_for_user(db, owner.id)  # per-user total, same each time it's set

    total_pending_rent = sum(
        _decimal_to_float(b.pending_amount)
        for b in db.query(Bill).join(Shop, Shop.id == Bill.shop_id)
        .filter(Shop.complex_id == id, Bill.bill_type == "Rent", Bill.status != "paid").all()
    )

    tenants = []
    for entry in tenants_by_user.values():
        entry["deposit_remaining"] = round(entry["deposit_required"] - entry["deposit_paid"], 2)
        entry["monthly_rent"] = round(entry["monthly_rent"], 2)
        entry["deposit_required"] = round(entry["deposit_required"], 2)
        entry["deposit_paid"] = round(entry["deposit_paid"], 2)
        entry["pending_rent"] = round(entry["pending_rent"], 2)
        tenants.append(entry)

    total_deposit_collected = sum(t["deposit_paid"] for t in tenants)

    return {
        "complex_id": c.id,
        "complex_name": c.name,
        "address": c.address,
        "stats": {
            "total_shops": len(shops),
            "occupied_shops": len(occupied),
            "available_shops": available_count,
            "total_monthly_rent": round(total_monthly_rent, 2),
            "total_pending_rent": round(total_pending_rent, 2),
            "total_deposit_required": round(total_deposit_required, 2),
            "total_deposit_collected": round(total_deposit_collected, 2),
            "total_deposit_remaining": round(total_deposit_required - total_deposit_collected, 2),
        },
        "tenants": tenants,
    }


@app.put("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def update_complex(
    id:   int,
    body: ComplexUpdate,
    db:   Session = Depends(get_db),
    actor:User    = Depends(require_admin),
):
    """Update a complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")

    old = {"name": obj.name, "address": obj.address, "description": obj.description}
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)

    write_audit(db, actor.id, "UPDATE", "complexes", id, old_data=old, new_data=body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(obj)
    return obj


@app.delete("/api/complex/{id}", tags=["Complex"])
def delete_complex(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Delete a complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")

    write_audit(db, actor.id, "DELETE", "complexes", id, old_data={"name": obj.name})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "Complex deleted"}


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Shop Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/shop", response_model=ShopResponse, status_code=201, tags=["Shop"])
def create_shop(
    body:  ShopCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Create a new shop. Admin only."""
    if body.complex_id:
        if not db.query(Complex).filter(Complex.id == body.complex_id).first():
            raise HTTPException(400, detail="Complex not found")

    obj = Shop(**body.model_dump())
    db.add(obj)
    db.flush()
    write_audit(db, actor.id, "CREATE", "shops", obj.id, new_data=body.model_dump())
    db.commit()
    db.refresh(obj)
    return _shop_to_response(obj, {})


@app.get("/api/shop", response_model=List[ShopResponse], tags=["Shop"])
def list_shops(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all shops, including current owner (if any). Admin only."""
    shops = db.query(Shop).order_by(Shop.id).all()
    owner_map = _shop_owner_map(db)
    return [_shop_to_response(s, owner_map) for s in shops]


@app.get("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def get_shop(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single shop, including current owner (if any). Admin only."""
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")
    owner_map = _shop_owner_map(db, [id])
    return _shop_to_response(obj, owner_map)


@app.put("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def update_shop(
    id:    int,
    body:  ShopUpdate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Update a shop. Admin only."""
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")

    old = {"shop_number": obj.shop_number, "status": obj.status, "complex_id": obj.complex_id}
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)

    write_audit(db, actor.id, "UPDATE", "shops", id, old_data=old, new_data=body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(obj)
    owner_map = _shop_owner_map(db, [id])
    return _shop_to_response(obj, owner_map)
@app.delete("/api/shop/{id}", tags=["Shop"])
def delete_shop(
    id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin)
):
    shop = db.query(Shop).filter(Shop.id == id).first()

    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    # Prevent deleting a shop that has bills
    if shop.bills:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete shop. {len(shop.bills)} bill(s) exist for this shop."
        )

    db.delete(shop)
    db.commit()

    return {
        "success": True,
        "message": f"Shop {id} deleted successfully"
    }

@app.post("/api/shop/{shop_id}/assign-complex", tags=["Shop"])
def assign_complex_to_shop(
    shop_id: int,
    body:    AssignComplexRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Assign a shop to a complex. One shop can belong to only one complex.
    Admin only.
    """
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")

    complex_obj = db.query(Complex).filter(Complex.id == body.complex_id).first()
    if not complex_obj:
        raise HTTPException(404, detail="Complex not found")

    old_complex_id  = shop.complex_id
    shop.complex_id = body.complex_id

    write_audit(
        db, actor.id, "UPDATE", "shops", shop_id,
        old_data={"complex_id": old_complex_id},
        new_data={"complex_id": body.complex_id},
    )
    db.commit()
    return {"success": True, "message": f"Shop {shop_id} assigned to complex {body.complex_id}"}


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: User Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/user", response_model=UserResponse, status_code=201, tags=["User"])
def create_user(
    body:  UserCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Create a new user (admin or tenant). Admin only."""
    if db.query(User).filter(User.mobile == body.mobile).first():
        raise HTTPException(400, detail="Mobile number already registered")

    if body.email and db.query(User).filter(User.email == body.email).first():
        raise HTTPException(400, detail="Email already registered")

    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()

    obj = User(
        name          = body.name,
        mobile        = body.mobile,
        email         = body.email,
        password_hash = password_hash,
        role          = body.role or "tenant",
        is_active     = True,
    )
    db.add(obj)
    db.flush()

    audit_data = body.model_dump(exclude={"password"})
    write_audit(db, actor.id, "CREATE", "users", obj.id, new_data=audit_data)
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/api/user", response_model=List[UserResponse], tags=["User"])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all users. Admin only."""
    return db.query(User).order_by(User.id).all()


@app.get("/api/user/{id}", response_model=UserResponse, tags=["User"])
def get_user(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single user. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")
    return obj


@app.put("/api/user/{id}", response_model=UserResponse, tags=["User"])
def update_user(
    id:    int,
    body:  UserUpdate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Update a user. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")

    old = {"name": obj.name, "mobile": obj.mobile, "role": obj.role, "is_active": obj.is_active}
    was_active = obj.is_active

    update_data = body.model_dump(exclude_unset=True)
    if "password" in update_data:
        obj.password_hash = bcrypt.hashpw(update_data.pop("password").encode(), bcrypt.gensalt()).decode()

    for field, value in update_data.items():
        setattr(obj, field, value)

    released_shops = []
    # Deactivation auto-releases all shops owned by this user back to "available".
    # Bills and payments already linked to this user are left untouched for records.
    if was_active and obj.is_active is False:
        owned_rows = db.query(UserShop).filter(UserShop.user_id == id).all()
        for row in owned_rows:
            shop = db.query(Shop).filter(Shop.id == row.shop_id).first()
            if shop:
                shop.status = "available"
            released_shops.append(row.shop_id)
            db.delete(row)

    write_audit(db, actor.id, "UPDATE", "users", id, old_data=old,
                new_data={**{k: v for k, v in update_data.items() if k != "password"},
                          "released_shops": released_shops} if released_shops
                          else {k: v for k, v in update_data.items() if k != "password"})
    db.commit()
    db.refresh(obj)
    return obj


@app.delete("/api/user/{id}", tags=["User"])
def delete_user(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Delete a user. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")
    if obj.id == actor.id:
        raise HTTPException(400, detail="Cannot delete your own account")

    write_audit(db, actor.id, "DELETE", "users", id, old_data={"mobile": obj.mobile, "role": obj.role})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "User deleted"}


@app.put("/api/user/{id}/reset-password", tags=["User"])
def reset_password(
    id:    int,
    body:  ResetPasswordRequest,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Admin resets a user's password without knowing the old one. Admin only."""
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")

    obj.password_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    write_audit(db, actor.id, "UPDATE", "users", id, new_data={"action": "password_reset"})
    db.commit()
    return {"message": "Password updated successfully"}


@app.get("/api/user/{id}/financial-summary", tags=["User"])
def user_financial_summary(
    id: int,
    db: Session = Depends(get_db),
    _:  User    = Depends(require_admin),
):
    """Full financial picture for a specific user/tenant. Admin only."""
    user = db.query(User).filter(User.id == id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    return _build_user_financial_summary(db, user)


@app.post("/api/user/{user_id}/assign-shops", tags=["User"])
def assign_shops_to_user(
    user_id: int,
    body:    AssignShopsRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Assign one or more shops to a user.

    A shop can have only ONE current owner at a time. If a requested shop is
    already owned by a different active user:
      - force=false (default): the whole request is rejected with 409,
        listing which shops are already taken and by whom, so the admin can
        confirm before reassigning.
      - force=true: the shop is detached from its previous owner first, then
        assigned to the new user (full reassignment).

    On success, every assigned shop's status is set to "occupied".
    Admin only.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    if not user.is_active:
        raise HTTPException(400, detail="Cannot assign shops to a deactivated user")

    # Validate shops exist first
    shops = {}
    for shop_id in body.shop_ids:
        shop = db.query(Shop).filter(Shop.id == shop_id).first()
        if not shop:
            raise HTTPException(400, detail=f"Shop {shop_id} not found")
        shops[shop_id] = shop

    owner_map = _shop_owner_map(db, body.shop_ids)

    # Find conflicts: shops already owned by a DIFFERENT user
    conflicts = {
        sid: owner for sid, owner in owner_map.items()
        if sid in shops and owner.id != user_id
    }

    if conflicts and not body.force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Some shops are already assigned to another tenant. "
                           "Resend with force=true to reassign.",
                "conflicts": [
                    {"shop_id": sid, "shop_number": shops[sid].shop_number,
                     "current_owner_id": owner.id, "current_owner_name": owner.name}
                    for sid, owner in conflicts.items()
                ],
            },
        )

    assigned = []
    reassigned_from = []
    for shop_id, shop in shops.items():
        # If force=true and shop has a different owner, detach the old owner first
        if shop_id in conflicts:
            old_row = db.query(UserShop).filter(UserShop.shop_id == shop_id).first()
            if old_row:
                db.delete(old_row)
                reassigned_from.append({"shop_id": shop_id, "from_user_id": conflicts[shop_id].id})

        exists = db.query(UserShop).filter(
            UserShop.user_id == user_id, UserShop.shop_id == shop_id
        ).first()
        if not exists:
            # Determine agreed_rent for this shop: per-shop override > flat
            # agreed_rent for the whole request > the shop's own shop_rent.
            if body.shop_rents and shop_id in body.shop_rents:
                rent_value = body.shop_rents[shop_id]
            elif body.agreed_rent is not None:
                rent_value = body.agreed_rent
            else:
                rent_value = _decimal_to_float(shop.shop_rent)
            db.add(UserShop(user_id=user_id, shop_id=shop_id, agreed_rent=Decimal(str(rent_value))))
            assigned.append(shop_id)

        shop.status = "occupied"

    write_audit(db, actor.id, "ASSIGN_SHOPS", "user_shops", user_id,
                old_data={"reassigned_from": reassigned_from} if reassigned_from else None,
                new_data={"user_id": user_id, "shop_ids": assigned})
    db.commit()
    return {
        "success": True,
        "message": f"Assigned shops {assigned} to user {user_id}",
        "reassigned_from": reassigned_from,
    }


@app.post("/api/user/{user_id}/detach-shops", tags=["User"])
def detach_shops_from_user(
    user_id: int,
    body:    DetachShopsRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """Detach one or more shops from a user and mark them available. Admin only."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")

    detached = []
    for shop_id in body.shop_ids:
        row = db.query(UserShop).filter(
            UserShop.user_id == user_id, UserShop.shop_id == shop_id
        ).first()
        if row:
            db.delete(row)
            detached.append(shop_id)
            shop = db.query(Shop).filter(Shop.id == shop_id).first()
            if shop:
                shop.status = "available"

    write_audit(db, actor.id, "DETACH_SHOPS", "user_shops", user_id,
                old_data={"user_id": user_id, "shop_ids": detached})
    db.commit()
    return {"success": True, "message": f"Detached shops {detached} from user {user_id}"}


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Bill Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/bill", response_model=BillResponse, status_code=201, tags=["Bill"])
def create_bill(
    body:  BillCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """
    Create a bill for a tenant.

    bill_type == "Rent": the bill amount is auto-filled from the agreed rent
    for this tenant/shop (UserShop.agreed_rent, falling back to the shop's
    shop_rent if no per-tenant rent was ever set). Any `amount` supplied in
    the request body is ignored for Rent bills — the server is the source of
    truth so rent always matches what was actually agreed.

    Any other bill_type (e.g. "Electricity", "Maintenance", "Other"):
    `amount` is required and used as-is. `description` is optional and is
    commonly used to clarify what the charge is for.
    Admin only.
    """
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(400, detail="User not found")
    shop = db.query(Shop).filter(Shop.id == body.shop_id).first()
    if not shop:
        raise HTTPException(400, detail="Shop not found")

    is_rent = body.bill_type.strip().lower() == "rent"

    if is_rent:
        user_shop = db.query(UserShop).filter(
            UserShop.user_id == body.user_id, UserShop.shop_id == body.shop_id
        ).first()
        if not user_shop:
            raise HTTPException(400, detail="This shop is not assigned to this user; cannot auto-fill rent.")
        amount_value = _agreed_rent_for(user_shop, shop)
        if amount_value <= 0:
            raise HTTPException(400, detail="Agreed rent for this tenant/shop is 0 or not set. Set shop_rent or agreed_rent first.")
    else:
        if body.amount is None or body.amount <= 0:
            raise HTTPException(400, detail="amount is required and must be greater than 0 for non-Rent bill types.")
        amount_value = body.amount

    amount = Decimal(str(amount_value))
    bill = Bill(
        user_id        = body.user_id,
        shop_id        = body.shop_id,
        bill_type      = body.bill_type,
        description    = body.description,
        amount         = amount,
        paid_amount    = Decimal("0"),
        pending_amount = amount,
        due_date       = body.due_date,
        status         = "pending",
    )
    db.add(bill)
    db.flush()

    write_audit(db, actor.id, "CREATE", "bills", bill.id, new_data={**body.model_dump(), "amount": float(amount)})
    db.commit()
    db.refresh(bill)
    return bill


@app.get("/api/bill", response_model=List[BillResponse], tags=["Bill"])
def list_bills(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all bills. Admin only."""
    return db.query(Bill).order_by(Bill.id).all()


@app.get("/api/bill/{id}", response_model=BillResponse, tags=["Bill"])
def get_bill(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single bill. Admin only."""
    obj = db.query(Bill).filter(Bill.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Bill not found")
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Payment Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/payment", response_model=PaymentResponse, status_code=201, tags=["Payment"])
def record_payment(
    body:  PaymentCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """
    Record a payment against a bill.
    Automatically updates paid_amount, pending_amount, and status.
    Admin only.
    """
    bill = db.query(Bill).filter(Bill.id == body.bill_id).first()
    if not bill:
        raise HTTPException(404, detail="Bill not found")

    if bill.status == "paid":
        raise HTTPException(400, detail="Bill is already fully paid")

    pay = Payment(
        bill_id        = body.bill_id,
        amount         = Decimal(str(body.amount)),
        payment_method = body.payment_method,
        remarks        = body.remarks,
    )
    db.add(pay)
    db.flush()

    # Reload payments to include the new record before reconciling
    db.refresh(bill)
    _reconcile_bill(bill)

    write_audit(db, actor.id, "CREATE", "payments", pay.id, new_data=body.model_dump())
    db.commit()
    db.refresh(pay)
    return pay


@app.get("/api/payment", response_model=List[PaymentResponse], tags=["Payment"])
def list_payments(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all payments. Admin only."""
    return db.query(Payment).order_by(Payment.id).all()


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Deposit Payment Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/deposit-payment", response_model=DepositPaymentResponse, status_code=201, tags=["Deposit Payment"])
def create_deposit_payment(
    body:  DepositPaymentCreate,
    db:    Session = Depends(get_db),
    actor: User    = Depends(require_admin),
):
    """Record a deposit payment for a tenant against a specific shop. Admin only."""
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    shop = db.query(Shop).filter(Shop.id == body.shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")

    user_shop = db.query(UserShop).filter(
        UserShop.user_id == body.user_id, UserShop.shop_id == body.shop_id
    ).first()
    if not user_shop:
        raise HTTPException(400, detail="Shop not assigned to this user")

    already_paid = _deposit_paid_for_shop(db, body.user_id, body.shop_id)
    deposit_required = _decimal_to_float(shop.shop_deposit)
    if already_paid + body.amount > deposit_required:
        raise HTTPException(400, detail=(
            f"Amount exceeds remaining deposit. Required={deposit_required}, "
            f"already paid={already_paid}, remaining={max(0.0, deposit_required - already_paid)}"
        ))

    dp = DepositPayment(
        user_id      = body.user_id,
        shop_id      = body.shop_id,
        amount       = Decimal(str(body.amount)),
        payment_date = body.payment_date or datetime.now(timezone.utc),
        remarks      = body.remarks,
    )
    db.add(dp)
    db.flush()
    write_audit(db, actor.id, "CREATE", "deposit_payments", dp.id, new_data=body.model_dump())
    db.commit()
    db.refresh(dp)
    return {
        "id": dp.id, "user_id": dp.user_id, "shop_id": dp.shop_id, "shop_number": shop.shop_number,
        "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
        "remarks": dp.remarks, "created_at": dp.created_at,
    }


@app.get("/api/deposit-payment", tags=["Deposit Payment"])
def list_deposit_payments(
    user_id:    Optional[int] = None,
    shop_id:    Optional[int] = None,
    complex_id: Optional[int] = None,
    db:         Session = Depends(get_db),
    _:          User    = Depends(require_admin),
):
    """List all deposit payments. Supports filters by user_id, shop_id, complex_id. Admin only."""
    q = db.query(DepositPayment, User, Shop).join(User, User.id == DepositPayment.user_id).join(Shop, Shop.id == DepositPayment.shop_id)
    if user_id is not None:
        q = q.filter(DepositPayment.user_id == user_id)
    if shop_id is not None:
        q = q.filter(DepositPayment.shop_id == shop_id)
    if complex_id is not None:
        q = q.filter(Shop.complex_id == complex_id)

    complexes = {c.id: c.name for c in db.query(Complex).all()}
    rows = q.order_by(DepositPayment.id).all()
    return [
        {
            "id": dp.id, "user_id": dp.user_id, "user_name": u.name,
            "shop_id": dp.shop_id, "shop_number": s.shop_number,
            "complex_id": s.complex_id, "complex_name": complexes.get(s.complex_id),
            "amount": _decimal_to_float(dp.amount), "payment_date": dp.payment_date,
            "remarks": dp.remarks, "created_at": dp.created_at,
        }
        for dp, u, s in rows
    ]


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Global Search
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/search", tags=["Search"])
def global_search(
    q:  str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _:  User    = Depends(require_admin),
):
    """Case-insensitive search across users, shops, and complexes. Admin only."""
    term = f"%{q.strip().lower()}%"

    users = db.query(User).filter(
        (User.name.ilike(term)) | (User.mobile.ilike(term))
    ).all()
    user_shops_map = {}
    if users:
        rows = (
            db.query(UserShop, Shop)
            .join(Shop, Shop.id == UserShop.shop_id)
            .filter(UserShop.user_id.in_([u.id for u in users]))
            .all()
        )
        for us, s in rows:
            user_shops_map.setdefault(us.user_id, []).append(s.shop_number)

    shops = db.query(Shop).filter(Shop.shop_number.ilike(term)).all()
    shop_ids = [s.id for s in shops]
    owner_map = _shop_owner_map(db, shop_ids) if shop_ids else {}
    complexes_by_id = {c.id: c.name for c in db.query(Complex).all()}

    complexes = db.query(Complex).filter(
        (Complex.name.ilike(term)) | (Complex.address.ilike(term))
    ).all()

    return {
        "users": [
            {
                "id": u.id, "name": u.name, "mobile": u.mobile, "email": u.email,
                "role": u.role, "is_active": u.is_active,
                "shops": user_shops_map.get(u.id, []),
            }
            for u in users
        ],
        "shops": [
            {
                "id": s.id, "shop_number": s.shop_number, "complex_id": s.complex_id,
                "complex_name": complexes_by_id.get(s.complex_id), "status": s.status,
                "shop_rent": _decimal_to_float(s.shop_rent), "shop_deposit": _decimal_to_float(s.shop_deposit),
                "assigned_to": owner_map.get(s.id).model_dump() if owner_map.get(s.id) else None,
            }
            for s in shops
        ],
        "complexes": [
            {"id": c.id, "name": c.name, "address": c.address}
            for c in complexes
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Reports
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports/summary", tags=["Reports"])
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
    all_bills = db.query(Bill).filter(Bill.status != "paid").all()
    outstanding = [
        {
            "bill_id": b.id,
            "user_id": b.user_id,
            "shop_id": b.shop_id,
            "bill_type": b.bill_type,
            "pending_amount": _decimal_to_float(b.pending_amount),
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


@app.get("/api/reports/rent-collection", tags=["Reports"])
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


@app.get("/api/reports/deposit", tags=["Reports"])
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


@app.get("/api/reports/occupancy", tags=["Reports"])
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
            us = db.query(UserShop).filter(UserShop.shop_id == s.id).order_by(UserShop.assigned_at.desc()).first()
            cdata["monthly_rent_actual"] += _agreed_rent_for(us, s) if us else rent
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


@app.get("/api/reports/user-wise", tags=["Reports"])
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

    results = []
    for u in users:
        user_shops = _current_user_shops(db, u.id)
        if complex_id is not None:
            shop_ids_in_complex = {s.id for s in db.query(Shop).filter(Shop.complex_id == complex_id).all()}
            user_shops = [us for us in user_shops if us.shop_id in shop_ids_in_complex]
            if not user_shops:
                continue

        shops_list = []
        deposit_required = deposit_paid = 0.0
        for us in user_shops:
            shop = db.query(Shop).filter(Shop.id == us.shop_id).first()
            if not shop:
                continue
            shops_list.append({
                "shop_number": shop.shop_number,
                "complex_name": complexes.get(shop.complex_id),
                "shop_rent": _agreed_rent_for(us, shop),
                "shop_deposit": _decimal_to_float(shop.shop_deposit),
            })
            deposit_required += _decimal_to_float(shop.shop_deposit)
            deposit_paid += _deposit_paid_for_shop(db, u.id, shop.id)

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


@app.get("/api/finance/overview", tags=["Reports"])
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
        entry["monthly_rent"] += _agreed_rent_for(us, s)
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


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Tenant Portal (read-only)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/tenant/profile", response_model=UserResponse, tags=["Tenant"])
def tenant_profile(current_user: User = Depends(require_tenant)):
    """Return the authenticated tenant's own profile."""
    return current_user


@app.get("/api/tenant/shops", tags=["Tenant"])
def tenant_shops(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return all shops assigned to the authenticated tenant."""
    rows = (
        db.query(Shop, UserShop)
        .join(UserShop, UserShop.shop_id == Shop.id)
        .filter(UserShop.user_id == current_user.id)
        .all()
    )
    return [
        {
            "id":           s.id,
            "shop_number":  s.shop_number,
            "area_sqft":    _decimal_to_float(s.area_sqft),
            "status":       s.status,
            "complex_id":   s.complex_id,
            "shop_rent":    _agreed_rent_for(us, s),
            "shop_deposit": _decimal_to_float(s.shop_deposit),
        }
        for s, us in rows
    ]


@app.get("/api/tenant/bills", tags=["Tenant"])
def tenant_bills(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return all bills for the authenticated tenant."""
    bills = db.query(Bill).filter(Bill.user_id == current_user.id).order_by(Bill.id).all()
    return [
        {
            "id":             b.id,
            "shop_id":        b.shop_id,
            "bill_type":      b.bill_type,
            "description":    b.description,
            "amount":         _decimal_to_float(b.amount),
            "paid_amount":    _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "bill_date":      b.bill_date,
            "due_date":       b.due_date,
            "status":         b.status,
        }
        for b in bills
    ]


@app.get("/api/tenant/payments", tags=["Tenant"])
def tenant_payments(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return all payments made by the authenticated tenant (via their bills)."""
    # Join Payment → Bill → filter by user
    payments = (
        db.query(Payment)
        .join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == current_user.id)
        .order_by(Payment.id)
        .all()
    )
    return [
        {
            "id":             p.id,
            "bill_id":        p.bill_id,
            "amount":         _decimal_to_float(p.amount),
            "payment_method": p.payment_method,
            "payment_date":   p.payment_date,
            "remarks":        p.remarks,
        }
        for p in payments
    ]


@app.get("/api/tenant/financial-summary", tags=["Tenant"])
def tenant_financial_summary(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(require_tenant),
):
    """Return the authenticated tenant's own full financial picture."""
    return _build_user_financial_summary(db, current_user)


# ══════════════════════════════════════════════════════════════════════════════
# Global exception handler – ensures JSON errors are always returned
# ══════════════════════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "detail": "Internal server error"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint (for direct `python app.py` execution)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)