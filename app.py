"""
    app.py - Main FastAPI Application
    Sahyadri Business Park — Tenant Management System

    Features:
        - JWT Authentication (HS256)
        - Role-Based Access Control (admin / tenant)
        - Full CRUD for complexes (with owner details), shops, users
        - Bill management: Rent auto-fill from agreed_rent; Other with free-text
        - Agreed rent stored on UserShop (monthly_rent field)
        - Payment reconciliation
        - Tenant read-only portal (shows complex owner contact details)
        - Complex reports (per-complex summary) + Bill reports
        - Users view shows assigned shops
        - Complaint management system with status tracking
        - Audit logging on every mutating operation
    """

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from db_config import get_db
from log import get_logger, log_request_middleware
from fastapi.middleware.cors import CORSMiddleware

from create_tables import (
    AuditLog, Bill, Complex, Payment, Shop, User, UserShop, Complaint,
)

logger = get_logger("app")

# ══════════════════════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════════════════════
app = FastAPI(
    title="Sahyadri Business Park",
    description="REST API for Sahyadri Business Park — tenant, shop, complex, bill and payment management.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.middleware("http")(log_request_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(log_request_middleware)

# ──────────────────────────────────────────────
# JWT Settings
# ──────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))

security = HTTPBearer()


# ══════════════════════════════════════════════════════════════════════════════
# JWT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token") from exc


# ══════════════════════════════════════════════════════════════════════════════
# AUTH DEPENDENCIES
# ══════════════════════════════════════════════════════════════════════════════

def get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(security),
        db: Session = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token missing subject")
    user = db.query(User).filter(User.id == int(user_id), User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def require_tenant(current_user: User = Depends(get_current_user)) -> User:
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
    entry = AuditLog(
        user_id=actor_id,
        action=action,
        table_name=table_name,
        record_id=record_id,
        old_data=json.dumps(old_data, default=str) if old_data else None,
        new_data=json.dumps(new_data, default=str) if new_data else None,
    )
    db.add(entry)


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

# ── Auth ──────────────────────────────────────
class LoginRequest(BaseModel):
    mobile: str = Field(..., example="9999999999")
    password: str = Field(..., example="admin@123")


class LoginResponse(BaseModel):
    success: bool
    token: str
    role: str


# ── Complex ───────────────────────────────────
class ComplexCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    address: Optional[str] = None
    description: Optional[str] = None
    owner_name: Optional[str] = Field(None, max_length=150)
    owner_contact: Optional[str] = Field(None, max_length=20)
    owner_address: Optional[str] = None


class ComplexUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    address: Optional[str] = None
    description: Optional[str] = None
    owner_name: Optional[str] = Field(None, max_length=150)
    owner_contact: Optional[str] = Field(None, max_length=20)
    owner_address: Optional[str] = None


class ComplexResponse(BaseModel):
    id: int
    name: str
    address: Optional[str]
    description: Optional[str]
    owner_name: Optional[str]
    owner_contact: Optional[str]
    owner_address: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Shop ──────────────────────────────────────
class ShopCreate(BaseModel):
    shop_number: str = Field(..., min_length=1, max_length=50)
    area_sqft: Optional[float] = None
    status: Optional[str] = Field("available", pattern="^(available|occupied|maintenance)$")
    complex_id: Optional[int] = None


class ShopUpdate(BaseModel):
    shop_number: Optional[str] = Field(None, min_length=1, max_length=50)
    area_sqft: Optional[float] = None
    status: Optional[str] = Field(None, pattern="^(available|occupied|maintenance)$")
    complex_id: Optional[int] = None


class ShopOwnerInfo(BaseModel):
    id: int
    name: str
    mobile: str


class ShopResponse(BaseModel):
    id: int
    shop_number: str
    area_sqft: Optional[float]
    status: str
    complex_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    assigned_to: Optional[ShopOwnerInfo] = None

    class Config:
        from_attributes = True


class AssignComplexRequest(BaseModel):
    complex_id: int


# ── User ──────────────────────────────────────
class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    mobile: str = Field(..., min_length=10, max_length=15)
    email: Optional[str] = None
    password: str = Field(..., min_length=6)
    role: Optional[str] = Field("tenant", pattern="^(admin|tenant)$")


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    mobile: Optional[str] = Field(None, min_length=10, max_length=15)
    email: Optional[str] = None
    password: Optional[str] = Field(None, min_length=6)
    role: Optional[str] = Field(None, pattern="^(admin|tenant)$")
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: int
    name: str
    mobile: str
    email: Optional[str]
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssignShopsRequest(BaseModel):
    shop_ids: List[int] = Field(..., min_length=1)
    force: bool = Field(False)
    monthly_rent: Optional[float] = Field(None, description="Agreed monthly rent for the assigned shop(s).")


class DetachShopsRequest(BaseModel):
    shop_ids: List[int] = Field(..., min_length=1)


# ── Bill ──────────────────────────────────────
class BillCreate(BaseModel):
    user_id: int
    shop_id: int
    bill_type: str = Field(..., min_length=1, max_length=80)
    amount: Optional[float] = Field(None, gt=0)  # nullable — auto-filled for Rent
    description: Optional[str] = None
    due_date: Optional[datetime] = None


class BillResponse(BaseModel):
    id: int
    user_id: int
    shop_id: int
    bill_type: str
    description: Optional[str]
    amount: float
    paid_amount: float
    pending_amount: float
    bill_date: datetime
    due_date: Optional[datetime]
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# ── Payment ───────────────────────────────────
class PaymentCreate(BaseModel):
    bill_id: int
    amount: float = Field(..., gt=0)
    payment_method: str = Field(..., min_length=1, max_length=60)
    remarks: Optional[str] = None


class PaymentResponse(BaseModel):
    id: int
    bill_id: int
    amount: float
    payment_method: str
    payment_date: datetime
    remarks: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ── Complaint ─────────────────────────────────
class ComplaintCreate(BaseModel):
    shop_id: Optional[int] = None
    category: str = Field(..., min_length=1, max_length=80)
    subject: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)


class ComplaintUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(pending|in_progress|resolved|rejected)$")
    admin_remarks: Optional[str] = None


class ComplaintResponse(BaseModel):
    id: int
    user_id: int
    shop_id: Optional[int]
    category: str
    subject: str
    description: str
    status: str
    admin_remarks: Optional[str]
    created_at: datetime
    updated_at: datetime
    user_name: Optional[str] = None
    shop_number: Optional[str] = None

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════════════════

def _decimal_to_float(value) -> float:
    return float(value) if isinstance(value, Decimal) else (value or 0.0)


def _shop_owner_map(db: Session, shop_ids: Optional[List[int]] = None) -> dict:
    q = db.query(UserShop, User).join(User, User.id == UserShop.user_id)
    if shop_ids is not None:
        q = q.filter(UserShop.shop_id.in_(shop_ids))
    rows = q.order_by(UserShop.shop_id, UserShop.assigned_at.desc()).all()
    owner_map = {}
    for user_shop, user in rows:
        if user_shop.shop_id not in owner_map:
            owner_map[user_shop.shop_id] = ShopOwnerInfo(id=user.id, name=user.name, mobile=user.mobile)
    return owner_map


def _shop_to_response(shop: Shop, owner_map: dict) -> ShopResponse:
    data = ShopResponse.model_validate(shop)
    data.assigned_to = owner_map.get(shop.id)
    return data


def _reconcile_bill(bill: Bill):
    total_paid = sum(_decimal_to_float(p.amount) for p in bill.payments)
    bill_amount = _decimal_to_float(bill.amount)
    bill.paid_amount = Decimal(str(total_paid))
    bill.pending_amount = Decimal(str(max(0.0, bill_amount - total_paid)))
    if total_paid <= 0:
        bill.status = "pending"
    elif total_paid >= bill_amount:
        bill.status = "paid"
    else:
        bill.status = "partial"


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTE: /api/login
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/login", response_model=LoginResponse, tags=["Authentication"])
def login(payload: LoginRequest, db: Session = Depends(get_db)):
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
def create_complex(body: ComplexCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Create a new complex with optional owner contact details. Admin only."""
    obj = Complex(**body.model_dump())
    db.add(obj)
    db.flush()
    write_audit(db, actor.id, "CREATE", "complexes", obj.id, new_data=body.model_dump())
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/api/complex", response_model=List[ComplexResponse], tags=["Complex"])
def list_complexes(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return db.query(Complex).order_by(Complex.id).all()


@app.get("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def get_complex(id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")
    return obj


@app.put("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def update_complex(id: int, body: ComplexUpdate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")
    old = {k: getattr(obj, k) for k in body.model_fields}
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    write_audit(db, actor.id, "UPDATE", "complexes", id, old_data=old, new_data=body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(obj)
    return obj


@app.delete("/api/complex/{id}", tags=["Complex"])
def delete_complex(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")
    write_audit(db, actor.id, "DELETE", "complexes", id, old_data={"name": obj.name})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "Complex deleted"}


# ── per-complex report ────────────────────────────────────────────────

@app.get("/api/complex/{id}/report", tags=["Complex"])
def complex_report(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    Detailed report for a single complex:
    total / assigned / unassigned shops, active / inactive tenants,
    total billed, total collected, outstanding dues.
    """
    complex_obj = db.query(Complex).filter(Complex.id == id).first()
    if not complex_obj:
        raise HTTPException(404, detail="Complex not found")

    shops = db.query(Shop).filter(Shop.complex_id == id).all()
    shop_ids = [s.id for s in shops]

    # User-shop assignments for this complex
    user_shops = db.query(UserShop).filter(UserShop.shop_id.in_(shop_ids)).all() if shop_ids else []
    assigned_shop_ids = {us.shop_id for us in user_shops}
    tenant_ids = list({us.user_id for us in user_shops})

    tenants = db.query(User).filter(User.id.in_(tenant_ids)).all() if tenant_ids else []
    active_tenants = [t for t in tenants if t.is_active]
    inactive_tenants = [t for t in tenants if not t.is_active]

    # Bills/payments for shops in this complex
    bills = db.query(Bill).filter(Bill.shop_id.in_(shop_ids)).all() if shop_ids else []
    total_billed = sum(_decimal_to_float(b.amount) for b in bills)
    total_collected = sum(_decimal_to_float(b.paid_amount) for b in bills)
    total_outstanding = sum(_decimal_to_float(b.pending_amount) for b in bills if b.status != "paid")

    # Build tenant-shop mapping
    shop_map = {s.id: s for s in shops}
    tenant_map = {t.id: t for t in tenants}
    tenant_detail = []
    for us in user_shops:
        t = tenant_map.get(us.user_id)
        s = shop_map.get(us.shop_id)
        if t and s:
            tenant_detail.append({
                "user_id": t.id,
                "name": t.name,
                "mobile": t.mobile,
                "is_active": t.is_active,
                "shop_id": s.id,
                "shop_number": s.shop_number,
                "monthly_rent": _decimal_to_float(us.monthly_rent) if hasattr(us,
                                                                              "monthly_rent") and us.monthly_rent else None,
                "assigned_at": us.assigned_at,
            })

    return {
        "complex": {
            "id": complex_obj.id,
            "name": complex_obj.name,
            "address": complex_obj.address,
            "owner_name": complex_obj.owner_name,
            "owner_contact": complex_obj.owner_contact,
            "owner_address": complex_obj.owner_address,
        },
        "shops": {
            "total": len(shops),
            "assigned": len(assigned_shop_ids),
            "unassigned": len(shops) - len(assigned_shop_ids),
            "occupied": sum(1 for s in shops if s.status == "occupied"),
            "available": sum(1 for s in shops if s.status == "available"),
            "maintenance": sum(1 for s in shops if s.status == "maintenance"),
        },
        "tenants": {
            "total": len(tenants),
            "active": len(active_tenants),
            "inactive": len(inactive_tenants),
            "details": tenant_detail,
        },
        "financials": {
            "total_billed": round(total_billed, 2),
            "total_collected": round(total_collected, 2),
            "total_outstanding": round(total_outstanding, 2),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Shop Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/shop", response_model=ShopResponse, status_code=201, tags=["Shop"])
def create_shop(body: ShopCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
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
    shops = db.query(Shop).order_by(Shop.id).all()
    owner_map = _shop_owner_map(db)
    return [_shop_to_response(s, owner_map) for s in shops]


@app.get("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def get_shop(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")
    owner_map = _shop_owner_map(db, [id])
    return _shop_to_response(obj, owner_map)


@app.put("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def update_shop(id: int, body: ShopUpdate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
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
def delete_shop(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")
    write_audit(db, actor.id, "DELETE", "shops", id, old_data={"shop_number": obj.shop_number})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "Shop deleted"}


@app.post("/api/shop/{shop_id}/assign-complex", tags=["Shop"])
def assign_complex_to_shop(shop_id: int, body: AssignComplexRequest, db: Session = Depends(get_db),
                           actor: User = Depends(require_admin)):
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")
    complex_obj = db.query(Complex).filter(Complex.id == body.complex_id).first()
    if not complex_obj:
        raise HTTPException(404, detail="Complex not found")
    old_complex_id = shop.complex_id
    shop.complex_id = body.complex_id
    write_audit(db, actor.id, "UPDATE", "shops", shop_id,
                old_data={"complex_id": old_complex_id}, new_data={"complex_id": body.complex_id})
    db.commit()
    return {"success": True, "message": f"Shop {shop_id} assigned to complex {body.complex_id}"}


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: User Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/user", response_model=UserResponse, status_code=201, tags=["User"])
def create_user(body: UserCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    if db.query(User).filter(User.mobile == body.mobile).first():
        raise HTTPException(400, detail="Mobile number already registered")
    if body.email and db.query(User).filter(User.email == body.email).first():
        raise HTTPException(400, detail="Email already registered")
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    obj = User(
        name=body.name, mobile=body.mobile, email=body.email,
        password_hash=password_hash, role=body.role or "tenant", is_active=True,
    )
    db.add(obj)
    db.flush()
    write_audit(db, actor.id, "CREATE", "users", obj.id, new_data=body.model_dump(exclude={"password"}))
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/api/user", response_model=List[UserResponse], tags=["User"])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return db.query(User).order_by(User.id).all()


@app.get("/api/user/{id}", response_model=UserResponse, tags=["User"])
def get_user(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")
    return obj


@app.get("/api/user/{id}/shops", tags=["User"])
def get_user_shops(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Return all shops currently assigned to a user, with complex info and monthly rent."""
    user = db.query(User).filter(User.id == id).first()
    if not user:
        raise HTTPException(404, detail="User not found")

    rows = (
        db.query(UserShop, Shop, Complex)
        .join(Shop, Shop.id == UserShop.shop_id)
        .outerjoin(Complex, Complex.id == Shop.complex_id)
        .filter(UserShop.user_id == id)
        .all()
    )

    result = []
    for us, shop, complex_obj in rows:
        result.append({
            "user_shop_id": us.id,
            "shop_id": shop.id,
            "shop_number": shop.shop_number,
            "area_sqft": _decimal_to_float(shop.area_sqft),
            "status": shop.status,
            "complex_id": shop.complex_id,
            "complex_name": complex_obj.name if complex_obj else None,
            "monthly_rent": _decimal_to_float(us.monthly_rent) if hasattr(us,
                                                                          "monthly_rent") and us.monthly_rent else None,
            "assigned_at": us.assigned_at,
        })
    return result


@app.put("/api/user/{id}", response_model=UserResponse, tags=["User"])
def update_user(id: int, body: UserUpdate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
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
    obj = db.query(User).filter(User.id == id).first()
    if not obj:
        raise HTTPException(404, detail="User not found")
    if obj.id == actor.id:
        raise HTTPException(400, detail="Cannot delete your own account")
    write_audit(db, actor.id, "DELETE", "users", id, old_data={"mobile": obj.mobile, "role": obj.role})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "User deleted"}


@app.post("/api/user/{user_id}/assign-shops", tags=["User"])
def assign_shops_to_user(
        user_id: int,
        body: AssignShopsRequest,
        db: Session = Depends(get_db),
        actor: User = Depends(require_admin),
):
    """
    Assign shops to a user.
    Pass monthly_rent to record the agreed rent for auto-fill on Rent bills.
    force=true reassigns shops currently owned by another tenant.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    if not user.is_active:
        raise HTTPException(400, detail="Cannot assign shops to a deactivated user")

    shops = {}
    for shop_id in body.shop_ids:
        shop = db.query(Shop).filter(Shop.id == shop_id).first()
        if not shop:
            raise HTTPException(400, detail=f"Shop {shop_id} not found")
        shops[shop_id] = shop

    owner_map = _shop_owner_map(db, body.shop_ids)
    conflicts = {
        sid: owner for sid, owner in owner_map.items()
        if sid in shops and owner.id != user_id
    }

    if conflicts and not body.force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Some shops are already assigned to another tenant. Resend with force=true to reassign.",
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
        if shop_id in conflicts:
            old_row = db.query(UserShop).filter(UserShop.shop_id == shop_id).first()
            if old_row:
                db.delete(old_row)
                reassigned_from.append({"shop_id": shop_id, "from_user_id": conflicts[shop_id].id})

        existing_row = db.query(UserShop).filter(
            UserShop.user_id == user_id, UserShop.shop_id == shop_id
        ).first()

        if existing_row:
            # Update rent if provided
            if body.monthly_rent is not None and hasattr(existing_row, "monthly_rent"):
                existing_row.monthly_rent = Decimal(str(body.monthly_rent))
        else:
            new_row = UserShop(user_id=user_id, shop_id=shop_id)
            if body.monthly_rent is not None and hasattr(new_row, "monthly_rent"):
                new_row.monthly_rent = Decimal(str(body.monthly_rent))
            db.add(new_row)
            assigned.append(shop_id)

        shop.status = "occupied"

    write_audit(db, actor.id, "ASSIGN_SHOPS", "user_shops", user_id,
                old_data={"reassigned_from": reassigned_from} if reassigned_from else None,
                new_data={"user_id": user_id, "shop_ids": assigned, "monthly_rent": body.monthly_rent})
    db.commit()
    return {
        "success": True,
        "message": f"Assigned shops {assigned} to user {user_id}",
        "reassigned_from": reassigned_from,
    }


@app.post("/api/user/{user_id}/detach-shops", tags=["User"])
def detach_shops_from_user(user_id: int, body: DetachShopsRequest, db: Session = Depends(get_db),
                           actor: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    detached = []
    for shop_id in body.shop_ids:
        row = db.query(UserShop).filter(UserShop.user_id == user_id, UserShop.shop_id == shop_id).first()
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


# ── get agreed rent for a user+shop ─────────────────────────────────────

@app.get("/api/user/{user_id}/shop/{shop_id}/rent", tags=["User"])
def get_agreed_rent(user_id: int, shop_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Return the agreed monthly rent for the given user-shop pairing."""
    row = db.query(UserShop).filter(UserShop.user_id == user_id, UserShop.shop_id == shop_id).first()
    if not row:
        raise HTTPException(404, detail="Assignment not found")
    rent = _decimal_to_float(row.monthly_rent) if hasattr(row, "monthly_rent") and row.monthly_rent else None
    return {"user_id": user_id, "shop_id": shop_id, "monthly_rent": rent}


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Bill Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/bill", response_model=BillResponse, status_code=201, tags=["Bill"])
def create_bill(body: BillCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """
    Create a bill.
    - bill_type='Rent': amount is auto-filled from user-shop agreed monthly_rent
      (the frontend may pass amount=None; backend will look it up).
    - bill_type='Other': amount must be provided manually.
    """
    if not db.query(User).filter(User.id == body.user_id).first():
        raise HTTPException(400, detail="User not found")
    if not db.query(Shop).filter(Shop.id == body.shop_id).first():
        raise HTTPException(400, detail="Shop not found")

    final_amount = body.amount

    if body.bill_type == "Rent":
        # Try to auto-fill from agreed rent
        us_row = db.query(UserShop).filter(
            UserShop.user_id == body.user_id,
            UserShop.shop_id == body.shop_id,
        ).first()
        if us_row and hasattr(us_row, "monthly_rent") and us_row.monthly_rent:
            final_amount = _decimal_to_float(us_row.monthly_rent)
        elif final_amount is None:
            raise HTTPException(400,
                                detail="No agreed rent found for this tenant-shop. Please set monthly_rent when assigning the shop, or enter the amount manually.")

    if final_amount is None or final_amount <= 0:
        raise HTTPException(400, detail="Amount is required and must be greater than 0.")

    amount = Decimal(str(final_amount))
    bill = Bill(
        user_id=body.user_id, shop_id=body.shop_id,
        bill_type=body.bill_type, description=body.description,
        amount=amount, paid_amount=Decimal("0"), pending_amount=amount,
        due_date=body.due_date, status="pending",
    )
    db.add(bill)
    db.flush()
    write_audit(db, actor.id, "CREATE", "bills", bill.id,
                new_data={**body.model_dump(), "resolved_amount": float(amount)})
    db.commit()
    db.refresh(bill)
    return bill


@app.get("/api/bill", response_model=List[BillResponse], tags=["Bill"])
def list_bills(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return db.query(Bill).order_by(Bill.id).all()


@app.get("/api/bill/{id}", response_model=BillResponse, tags=["Bill"])
def get_bill(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    obj = db.query(Bill).filter(Bill.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Bill not found")
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Payment Management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/payment", response_model=PaymentResponse, status_code=201, tags=["Payment"])
def record_payment(body: PaymentCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    bill = db.query(Bill).filter(Bill.id == body.bill_id).first()
    if not bill:
        raise HTTPException(404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(400, detail="Bill is already fully paid")
    pay = Payment(
        bill_id=body.bill_id, amount=Decimal(str(body.amount)),
        payment_method=body.payment_method, remarks=body.remarks,
    )
    db.add(pay)
    db.flush()
    db.refresh(bill)
    _reconcile_bill(bill)
    write_audit(db, actor.id, "CREATE", "payments", pay.id, new_data=body.model_dump())
    db.commit()
    db.refresh(pay)
    return pay


@app.get("/api/payment", response_model=List[PaymentResponse], tags=["Payment"])
def list_payments(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return db.query(Payment).order_by(Payment.id).all()


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Complaints
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/complaint", response_model=ComplaintResponse, status_code=201, tags=["Complaint"])
def create_complaint(
        body: ComplaintCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(require_tenant),
):
    """Tenant raises a new complaint."""
    # Verify shop exists if provided
    if body.shop_id:
        shop = db.query(Shop).filter(Shop.id == body.shop_id).first()
        if not shop:
            raise HTTPException(400, detail="Shop not found")
        # Verify the shop belongs to the tenant
        assignment = db.query(UserShop).filter(
            UserShop.user_id == current_user.id,
            UserShop.shop_id == body.shop_id
        ).first()
        if not assignment:
            raise HTTPException(400, detail="You are not assigned to this shop")

    complaint = Complaint(
        user_id=current_user.id,
        shop_id=body.shop_id,
        category=body.category,
        subject=body.subject,
        description=body.description,
        status="pending",
    )
    db.add(complaint)
    db.flush()
    write_audit(db, current_user.id, "CREATE", "complaints", complaint.id, new_data=body.model_dump())
    db.commit()
    db.refresh(complaint)

    # Enrich response with user/shop info
    result = ComplaintResponse.model_validate(complaint)
    result.user_name = current_user.name
    if body.shop_id:
        shop = db.query(Shop).filter(Shop.id == body.shop_id).first()
        result.shop_number = shop.shop_number if shop else None
    return result


@app.get("/api/complaint", response_model=List[ComplaintResponse], tags=["Complaint"])
def list_complaints(
        db: Session = Depends(get_db),
        _: User = Depends(require_admin),
):
    """Admin sees all complaints."""
    complaints = db.query(Complaint).order_by(Complaint.created_at.desc()).all()
    result = []
    user_ids = {c.user_id for c in complaints}
    shop_ids = {c.shop_id for c in complaints if c.shop_id}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    shops = {s.id: s for s in db.query(Shop).filter(Shop.id.in_(shop_ids)).all()} if shop_ids else {}

    for c in complaints:
        r = ComplaintResponse.model_validate(c)
        r.user_name = users.get(c.user_id).name if users.get(c.user_id) else None
        if c.shop_id and c.shop_id in shops:
            r.shop_number = shops[c.shop_id].shop_number
        result.append(r)
    return result


@app.get("/api/complaint/tenant", response_model=List[ComplaintResponse], tags=["Complaint"])
def tenant_list_complaints(
        db: Session = Depends(get_db),
        current_user: User = Depends(require_tenant),
):
    """Tenant sees only their own complaints."""
    complaints = db.query(Complaint).filter(
        Complaint.user_id == current_user.id
    ).order_by(Complaint.created_at.desc()).all()

    result = []
    shop_ids = {c.shop_id for c in complaints if c.shop_id}
    shops = {s.id: s for s in db.query(Shop).filter(Shop.id.in_(shop_ids)).all()} if shop_ids else {}

    for c in complaints:
        r = ComplaintResponse.model_validate(c)
        r.user_name = current_user.name
        if c.shop_id and c.shop_id in shops:
            r.shop_number = shops[c.shop_id].shop_number
        result.append(r)
    return result


@app.get("/api/complaint/{id}", response_model=ComplaintResponse, tags=["Complaint"])
def get_complaint(
        id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    """Get a single complaint. Admins can see any; tenants only their own."""
    complaint = db.query(Complaint).filter(Complaint.id == id).first()
    if not complaint:
        raise HTTPException(404, detail="Complaint not found")

    # Check access
    if current_user.role != "admin" and complaint.user_id != current_user.id:
        raise HTTPException(403, detail="Access denied")

    result = ComplaintResponse.model_validate(complaint)
    user = db.query(User).filter(User.id == complaint.user_id).first()
    result.user_name = user.name if user else None
    if complaint.shop_id:
        shop = db.query(Shop).filter(Shop.id == complaint.shop_id).first()
        result.shop_number = shop.shop_number if shop else None
    return result


@app.put("/api/complaint/{id}", response_model=ComplaintResponse, tags=["Complaint"])
def update_complaint(
        id: int,
        body: ComplaintUpdate,
        db: Session = Depends(get_db),
        actor: User = Depends(require_admin),
):
    """Admin updates complaint status and adds remarks."""
    complaint = db.query(Complaint).filter(Complaint.id == id).first()
    if not complaint:
        raise HTTPException(404, detail="Complaint not found")

    old_data = {"status": complaint.status, "admin_remarks": complaint.admin_remarks}

    if body.status is not None:
        complaint.status = body.status
    if body.admin_remarks is not None:
        complaint.admin_remarks = body.admin_remarks

    write_audit(db, actor.id, "UPDATE", "complaints", id, old_data=old_data, new_data=body.model_dump())
    db.commit()
    db.refresh(complaint)

    result = ComplaintResponse.model_validate(complaint)
    user = db.query(User).filter(User.id == complaint.user_id).first()
    result.user_name = user.name if user else None
    if complaint.shop_id:
        shop = db.query(Shop).filter(Shop.id == complaint.shop_id).first()
        result.shop_number = shop.shop_number if shop else None
    return result


@app.delete("/api/complaint/{id}", tags=["Complaint"])
def delete_complaint(
        id: int,
        db: Session = Depends(get_db),
        actor: User = Depends(require_admin),
):
    """Admin can delete a complaint."""
    complaint = db.query(Complaint).filter(Complaint.id == id).first()
    if not complaint:
        raise HTTPException(404, detail="Complaint not found")

    write_audit(db, actor.id, "DELETE", "complaints", id, old_data={"subject": complaint.subject})
    db.delete(complaint)
    db.commit()
    return {"success": True, "message": "Complaint deleted"}


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Reports
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports/summary", tags=["Reports"])
def reports_summary(
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        db: Session = Depends(get_db),
        _: User = Depends(require_admin),
):
    """Bill-level business summary report. Admin only."""
    shops = db.query(Shop).all()
    total_shops = len(shops)
    occupied = sum(1 for s in shops if s.status == "occupied")
    available = sum(1 for s in shops if s.status == "available")
    maintenance = sum(1 for s in shops if s.status == "maintenance")

    bill_q = db.query(Bill)
    if start_date:
        bill_q = bill_q.filter(Bill.bill_date >= start_date)
    if end_date:
        bill_q = bill_q.filter(Bill.bill_date <= end_date)
    bills_in_range = bill_q.all()

    total_billed = sum(_decimal_to_float(b.amount) for b in bills_in_range)
    total_pending_in_range = sum(_decimal_to_float(b.pending_amount) for b in bills_in_range)

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

    all_bills = db.query(Bill).filter(Bill.status != "paid").all()
    outstanding = [
        {
            "bill_id": b.id, "user_id": b.user_id, "shop_id": b.shop_id,
            "bill_type": b.bill_type, "pending_amount": _decimal_to_float(b.pending_amount),
            "due_date": b.due_date, "status": b.status,
        }
        for b in all_bills
    ]

    return {
        "range": {"start_date": start_date, "end_date": end_date},
        "occupancy": {
            "total_shops": total_shops, "occupied": occupied,
            "available": available, "maintenance": maintenance,
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


@app.get("/api/reports/complexes", tags=["Reports"])
def reports_all_complexes(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Summary stats for every complex. Admin only."""
    complexes = db.query(Complex).order_by(Complex.id).all()
    result = []
    for c in complexes:
        shops = db.query(Shop).filter(Shop.complex_id == c.id).all()
        shop_ids = [s.id for s in shops]
        user_shops = db.query(UserShop).filter(UserShop.shop_id.in_(shop_ids)).all() if shop_ids else []
        tenant_ids = list({us.user_id for us in user_shops})
        tenants = db.query(User).filter(User.id.in_(tenant_ids)).all() if tenant_ids else []
        bills = db.query(Bill).filter(Bill.shop_id.in_(shop_ids)).all() if shop_ids else []
        result.append({
            "complex_id": c.id,
            "complex_name": c.name,
            "owner_name": c.owner_name,
            "owner_contact": c.owner_contact,
            "total_shops": len(shops),
            "assigned_shops": len({us.shop_id for us in user_shops}),
            "total_tenants": len(tenants),
            "active_tenants": sum(1 for t in tenants if t.is_active),
            "total_billed": round(sum(_decimal_to_float(b.amount) for b in bills), 2),
            "total_collected": round(sum(_decimal_to_float(b.paid_amount) for b in bills), 2),
            "outstanding": round(sum(_decimal_to_float(b.pending_amount) for b in bills if b.status != "paid"), 2),
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# ── ROUTES: Tenant Portal (read-only)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/tenant/profile", response_model=UserResponse, tags=["Tenant"])
def tenant_profile(current_user: User = Depends(require_tenant)):
    return current_user


@app.get("/api/tenant/shops", tags=["Tenant"])
def tenant_shops(db: Session = Depends(get_db), current_user: User = Depends(require_tenant)):
    """Return all shops assigned to the authenticated tenant, including complex owner contact."""
    rows = (
        db.query(UserShop, Shop, Complex)
        .join(Shop, Shop.id == UserShop.shop_id)
        .outerjoin(Complex, Complex.id == Shop.complex_id)
        .filter(UserShop.user_id == current_user.id)
        .all()
    )
    result = []
    for us, s, c in rows:
        result.append({
            "id": s.id,
            "shop_number": s.shop_number,
            "area_sqft": _decimal_to_float(s.area_sqft),
            "status": s.status,
            "complex_id": s.complex_id,
            "complex_name": c.name if c else None,
            "complex_address": c.address if c else None,
            "owner_name": c.owner_name if c else None,
            "owner_contact": c.owner_contact if c else None,
            "owner_address": c.owner_address if c else None,
            "monthly_rent": _decimal_to_float(us.monthly_rent) if hasattr(us,
                                                                          "monthly_rent") and us.monthly_rent else None,
        })
    return result


@app.get("/api/tenant/bills", tags=["Tenant"])
def tenant_bills(db: Session = Depends(get_db), current_user: User = Depends(require_tenant)):
    bills = db.query(Bill).filter(Bill.user_id == current_user.id).order_by(Bill.id).all()
    return [
        {
            "id": b.id, "shop_id": b.shop_id, "bill_type": b.bill_type,
            "description": b.description, "amount": _decimal_to_float(b.amount),
            "paid_amount": _decimal_to_float(b.paid_amount),
            "pending_amount": _decimal_to_float(b.pending_amount),
            "bill_date": b.bill_date, "due_date": b.due_date, "status": b.status,
        }
        for b in bills
    ]


@app.get("/api/tenant/payments", tags=["Tenant"])
def tenant_payments(db: Session = Depends(get_db), current_user: User = Depends(require_tenant)):
    payments = (
        db.query(Payment)
        .join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.user_id == current_user.id)
        .order_by(Payment.id)
        .all()
    )
    return [
        {
            "id": p.id, "bill_id": p.bill_id, "amount": _decimal_to_float(p.amount),
            "payment_method": p.payment_method, "payment_date": p.payment_date, "remarks": p.remarks,
        }
        for p in payments
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Global exception handler
# ══════════════════════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"success": False, "detail": "Internal server error"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)