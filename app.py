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
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from db_config import get_db
from log import get_logger, log_request_middleware
from fastapi.middleware.cors import CORSMiddleware


# Import ORM models from create_tables so we have a single schema source-of-truth
from create_tables import (
    AuditLog, Bill, Complex, Payment, Shop, User, UserShop,
)

# ──────────────────────────────────────────────
# Logger
# ──────────────────────────────────────────────
logger = get_logger("app")

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

# Register request-logging middleware
app.middleware("http")(log_request_middleware)

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
    shop_number: str             = Field(..., min_length=1, max_length=50)
    area_sqft:   Optional[float] = None
    status:      Optional[str]   = Field("available", pattern="^(available|occupied|maintenance)$")
    complex_id:  Optional[int]   = None


class ShopUpdate(BaseModel):
    shop_number: Optional[str]   = Field(None, min_length=1, max_length=50)
    area_sqft:   Optional[float] = None
    status:      Optional[str]   = Field(None, pattern="^(available|occupied|maintenance)$")
    complex_id:  Optional[int]   = None


class ShopResponse(BaseModel):
    id:          int
    shop_number: str
    area_sqft:   Optional[float]
    status:      str
    complex_id:  Optional[int]
    created_at:  datetime
    updated_at:  datetime

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
    shop_ids: List[int] = Field(..., min_length=1)


class DetachShopsRequest(BaseModel):
    shop_ids: List[int] = Field(..., min_length=1)


# ── Bill ──────────────────────────────────────
class BillCreate(BaseModel):
    user_id:     int
    shop_id:     int
    bill_type:   str            = Field(..., min_length=1, max_length=80)
    amount:      float          = Field(..., gt=0)
    description: Optional[str] = None
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


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════════════════

def _decimal_to_float(value) -> float:
    """Convert Decimal to float for Pydantic serialisation."""
    return float(value) if isinstance(value, Decimal) else (value or 0.0)


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


@app.get("/api/complex/{id}", response_model=ComplexResponse, tags=["Complex"])
def get_complex(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single complex. Admin only."""
    obj = db.query(Complex).filter(Complex.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Complex not found")
    return obj


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
    return obj


@app.get("/api/shop", response_model=List[ShopResponse], tags=["Shop"])
def list_shops(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List all shops. Admin only."""
    return db.query(Shop).order_by(Shop.id).all()


@app.get("/api/shop/{id}", response_model=ShopResponse, tags=["Shop"])
def get_shop(id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Retrieve a single shop. Admin only."""
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")
    return obj


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
    return obj


@app.delete("/api/shop/{id}", tags=["Shop"])
def delete_shop(id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Delete a shop. Admin only."""
    obj = db.query(Shop).filter(Shop.id == id).first()
    if not obj:
        raise HTTPException(404, detail="Shop not found")

    write_audit(db, actor.id, "DELETE", "shops", id, old_data={"shop_number": obj.shop_number})
    db.delete(obj)
    db.commit()
    return {"success": True, "message": "Shop deleted"}


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

    update_data = body.model_dump(exclude_unset=True)
    if "password" in update_data:
        obj.password_hash = bcrypt.hashpw(update_data.pop("password").encode(), bcrypt.gensalt()).decode()

    for field, value in update_data.items():
        setattr(obj, field, value)

    write_audit(db, actor.id, "UPDATE", "users", id, old_data=old,
                new_data={k: v for k, v in update_data.items() if k != "password"})
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


@app.post("/api/user/{user_id}/assign-shops", tags=["User"])
def assign_shops_to_user(
    user_id: int,
    body:    AssignShopsRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """
    Assign one or more shops to a user.
    One user can have multiple shops; duplicate assignments are silently skipped.
    Admin only.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="User not found")

    assigned = []
    for shop_id in body.shop_ids:
        shop = db.query(Shop).filter(Shop.id == shop_id).first()
        if not shop:
            raise HTTPException(400, detail=f"Shop {shop_id} not found")

        exists = db.query(UserShop).filter(
            UserShop.user_id == user_id, UserShop.shop_id == shop_id
        ).first()
        if not exists:
            db.add(UserShop(user_id=user_id, shop_id=shop_id))
            assigned.append(shop_id)

    write_audit(db, actor.id, "ASSIGN_SHOPS", "user_shops", user_id,
                new_data={"user_id": user_id, "shop_ids": assigned})
    db.commit()
    return {"success": True, "message": f"Assigned shops {assigned} to user {user_id}"}


@app.post("/api/user/{user_id}/detach-shops", tags=["User"])
def detach_shops_from_user(
    user_id: int,
    body:    DetachShopsRequest,
    db:      Session = Depends(get_db),
    actor:   User    = Depends(require_admin),
):
    """Detach one or more shops from a user. Admin only."""
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
    """Create a bill for a tenant. Admin only."""
    if not db.query(User).filter(User.id == body.user_id).first():
        raise HTTPException(400, detail="User not found")
    if not db.query(Shop).filter(Shop.id == body.shop_id).first():
        raise HTTPException(400, detail="Shop not found")

    amount = Decimal(str(body.amount))
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

    write_audit(db, actor.id, "CREATE", "bills", bill.id, new_data=body.model_dump())
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
        db.query(Shop)
        .join(UserShop, UserShop.shop_id == Shop.id)
        .filter(UserShop.user_id == current_user.id)
        .all()
    )
    return [
        {
            "id":          s.id,
            "shop_number": s.shop_number,
            "area_sqft":   _decimal_to_float(s.area_sqft),
            "status":      s.status,
            "complex_id":  s.complex_id,
        }
        for s in rows
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
