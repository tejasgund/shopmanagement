"""
================================================================================
 SHOP ELECTRICITY BILL MANAGEMENT SYSTEM
================================================================================
 Framework   : FastAPI
 ORM         : SQLAlchemy 2.x (declarative models)
 Database    : MySQL, accessed through the connection pool in dbconfig.py
 Auth        : JWT (python-jose) + bcrypt password hashing (passlib)
 Logging     : log.py (centralized logger)

 PROJECT LAYOUT
 ---------------------------------------------------------------------------
   dbconfig.py   -> mysql-connector-python connection pool
   log.py        -> centralized logger factory
   main.py       -> (this file) all models, schemas, auth, services & routes
   createMN.py   -> stand-alone script that creates/updates all DB tables

 HOW TO RUN
 ---------------------------------------------------------------------------
   1) python createMN.py          # creates all tables (only if missing)
   2) uvicorn main:app --reload   # starts the API server

 DEFAULT ADMIN (auto-created on first startup if it doesn't exist)
   username: admin
   password: admin123
================================================================================
"""

# ------------------------------------------------------------------------------
# Standard / third-party imports
# ------------------------------------------------------------------------------
from datetime import datetime, date, timedelta
from enum import Enum as PyEnum
from typing import Optional, List, Generic, TypeVar

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Date, DateTime,
    Text, ForeignKey, Enum as SAEnum, UniqueConstraint, func,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError

from jose import JWTError, jwt
from passlib.context import CryptContext

# ------------------------------------------------------------------------------
# Local imports - connection pool & logger (provided by the project)
# ------------------------------------------------------------------------------
from dbconfig import get_connection
from log import get_logger

logger = get_logger("main")


# ==============================================================================
# DATABASE ENGINE
# ==============================================================================
# We build the SQLAlchemy engine on top of the mysql-connector connection
# pool defined in dbconfig.py by passing `creator=get_connection`. Every
# connection SQLAlchemy uses is therefore pulled from that pool, while we
# still get the full SQLAlchemy ORM (sessions, models, relationships, query
# building, etc.) on top of it.
#
# Example plain SQLAlchemy connection string (if you ever want to bypass the
# custom pool and let SQLAlchemy manage its own pool instead), kept here for
# reference only:
#   "mysql+mysqlconnector://admin:admin@172.31.52.221:3306/test_lightbill"
engine = create_engine(
    "mysql+mysqlconnector://",
    creator=get_connection,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ==============================================================================
# ENUMS (shared between SQLAlchemy models and Pydantic schemas)
# ==============================================================================
class UserRole(str, PyEnum):
    ADMIN = "admin"
    USER = "user"


class PaymentStatus(str, PyEnum):
    PENDING = "pending"
    PAID = "paid"


# ==============================================================================
# SQLALCHEMY MODELS (declarative ORM models -> DB tables)
# ==============================================================================
class User(Base):
    """Application users. Admins manage the system; users own shops & bills."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(150), nullable=False)
    mobile_number = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    deposit = Column(Float, default=0.0, nullable=False)
    monthly_rent = Column(Float, default=0.0, nullable=False)
    role = Column(SAEnum(UserRole), default=UserRole.USER, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    shop_links = relationship("UserShop", back_populates="user", cascade="all, delete-orphan")
    bills = relationship("Bill", back_populates="user")


class Shop(Base):
    """A shop that has its own electricity sub-meter."""
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    shop_name = Column(String(150), nullable=False)
    shop_code = Column(String(50), unique=True, nullable=False, index=True)
    sub_meter_number = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user_links = relationship("UserShop", back_populates="shop", cascade="all, delete-orphan")
    readings = relationship("MeterReading", back_populates="shop")
    bills = relationship("Bill", back_populates="shop")


class UserShop(Base):
    """
    Junction table assigning a shop to a user.
    Business rule: a shop is assigned to exactly ONE user (enforced via the
    unique constraint on shop_id below), while a user may own many shops.
    """
    __tablename__ = "user_shops"
    __table_args__ = (UniqueConstraint("shop_id", name="uq_shop_single_owner"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    assigned_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="shop_links")
    shop = relationship("Shop", back_populates="user_links")


class ElectricityRate(Base):
    """Historical record of per-unit electricity rates set by an admin."""
    __tablename__ = "electricity_rates"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    unit_rate = Column(Float, nullable=False)
    effective_from = Column(Date, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class MeterReading(Base):
    """Monthly sub-meter reading submitted by a shop's owning user."""
    __tablename__ = "meter_readings"
    __table_args__ = (
        UniqueConstraint("shop_id", "reading_year", "reading_month", name="uq_one_reading_per_month"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    reading_year = Column(Integer, nullable=False)
    reading_month = Column(Integer, nullable=False)
    previous_reading = Column(Float, nullable=False)
    current_reading = Column(Float, nullable=False)
    used_units = Column(Float, nullable=False)
    unit_rate = Column(Float, nullable=False)
    electricity_bill = Column(Float, nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    shop = relationship("Shop", back_populates="readings")


class Bill(Base):
    """Auto-generated monthly bill (electricity + rent) tied to a reading."""
    __tablename__ = "bills"
    __table_args__ = (
        UniqueConstraint("shop_id", "bill_year", "bill_month", name="uq_one_bill_per_month"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    bill_year = Column(Integer, nullable=False)
    bill_month = Column(Integer, nullable=False)
    rent_amount = Column(Float, nullable=False, default=0.0)
    used_units = Column(Float, nullable=False)
    unit_rate = Column(Float, nullable=False)
    electricity_bill = Column(Float, nullable=False)
    total_bill = Column(Float, nullable=False)
    payment_status = Column(SAEnum(PaymentStatus), default=PaymentStatus.PENDING, nullable=False)
    payment_date = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="bills")
    shop = relationship("Shop", back_populates="bills")


# ==============================================================================
# PYDANTIC SCHEMAS (request validation & response models)
# ==============================================================================
T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic wrapper used for paginated list endpoints."""
    total: int
    skip: int
    limit: int
    items: List[T]


# ---- Auth -------------------------------------------------------------------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---- Users --------------------------------------------------------------------
MOBILE_PATTERN = r"^\+?[0-9]{7,15}$"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    full_name: str = Field(..., min_length=1, max_length=150)
    mobile_number: Optional[str] = Field(None, max_length=20, pattern=MOBILE_PATTERN)
    address: Optional[str] = None
    deposit: float = Field(0.0, ge=0)
    monthly_rent: float = Field(0.0, ge=0)
    role: UserRole = UserRole.USER
    is_active: bool = True


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=150)
    mobile_number: Optional[str] = Field(None, max_length=20, pattern=MOBILE_PATTERN)
    address: Optional[str] = None
    deposit: Optional[float] = Field(None, ge=0)
    monthly_rent: Optional[float] = Field(None, ge=0)
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=6)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str
    mobile_number: Optional[str] = None
    address: Optional[str] = None
    deposit: float
    monthly_rent: float
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None


# ---- Shops --------------------------------------------------------------------
class ShopCreate(BaseModel):
    shop_name: str = Field(..., min_length=1, max_length=150)
    shop_code: str = Field(..., min_length=1, max_length=50)
    sub_meter_number: str = Field(..., min_length=1, max_length=50)
    description: Optional[str] = None


class ShopUpdate(BaseModel):
    shop_name: Optional[str] = Field(None, min_length=1, max_length=150)
    shop_code: Optional[str] = Field(None, min_length=1, max_length=50)
    sub_meter_number: Optional[str] = Field(None, min_length=1, max_length=50)
    description: Optional[str] = None


class ShopResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shop_name: str
    shop_code: str
    sub_meter_number: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


# ---- Shop assignments -----------------------------------------------------
class ShopAssignmentCreate(BaseModel):
    user_id: int
    shop_id: int


class UserShopResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    shop_id: int
    assigned_at: datetime


# ---- Electricity rates ----------------------------------------------------
class RateCreate(BaseModel):
    unit_rate: float = Field(..., gt=0)
    effective_from: date


class RateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    unit_rate: float
    effective_from: date
    created_by: int
    created_at: datetime


# ---- Meter readings ---------------------------------------------------------
class ReadingCreate(BaseModel):
    shop_id: int
    reading_year: int = Field(..., ge=2000, le=2100)
    reading_month: int = Field(..., ge=1, le=12)
    current_reading: float = Field(..., ge=0)
    # Only used for the very first reading of a shop (no history to infer from).
    # For every subsequent reading, the previous reading is fetched automatically.
    previous_reading: Optional[float] = Field(None, ge=0)


class ReadingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shop_id: int
    reading_year: int
    reading_month: int
    previous_reading: float
    current_reading: float
    used_units: float
    unit_rate: float
    electricity_bill: float
    submitted_by: int
    created_at: datetime


# ---- Bills --------------------------------------------------------------------
class BillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    shop_id: int
    bill_year: int
    bill_month: int
    rent_amount: float
    used_units: float
    unit_rate: float
    electricity_bill: float
    total_bill: float
    payment_status: PaymentStatus
    payment_date: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime


# ==============================================================================
# SECURITY: password hashing & JWT helpers
# ==============================================================================
# NOTE: move SECRET_KEY to an environment variable in production.
# NOTE: passlib's bcrypt backend requires bcrypt==4.0.1 (see requirements.txt) -
#       newer bcrypt 4.1+ releases break passlib's backend auto-detection.
SECRET_KEY = "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # 8 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a signed JWT access token embedding the given claims."""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ==============================================================================
# DEPENDENCY INJECTION
# ==============================================================================
def get_db():
    """Yields a SQLAlchemy session per-request and always closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """Decode the JWT bearer token and load the corresponding active user."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Role-based authorization guard: only allows 'admin' role through."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return current_user


def _get_owned_shop_or_403(db: Session, shop_id: int, user: User) -> Shop:
    """Ensure `shop_id` is assigned to `user`; raise 403/404 otherwise."""
    link = db.query(UserShop).filter(UserShop.shop_id == shop_id, UserShop.user_id == user.id).first()
    if not link:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This shop is not assigned to you")
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop not found")
    return shop


# ==============================================================================
# FASTAPI APP
# ==============================================================================
app = FastAPI(
    title="Shop Electricity Bill Management System",
    description="Backend API for managing shops, sub-meter readings, and auto-generated bills.",
    version="1.0.0",
)

# Allow all origins (development only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                # ✅ allows any domain
    allow_credentials=True,
    allow_methods=["*"],                # allows GET, POST, PUT, DELETE, OPTIONS
    allow_headers=["*"],                # allows Authorization and other headers
)


# ---- Global error handlers (clean, consistent JSON error responses) ---------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    logger.warning(f"Validation error on {request.url.path}: {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(SQLAlchemyError)
async def db_exception_handler(request, exc: SQLAlchemyError):
    logger.error(f"Database error on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "A database error occurred"})


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception):
    logger.error(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred"})


# ---- Startup: verify schema & seed default admin -----------------------------
@app.on_event("startup")
def on_startup():
    """
    Runs once when the ASGI server starts.
    1) Ensures all tables exist (safety net - createMN.py is the dedicated,
       stand-alone script for this and can be run independently/in CI).
    2) Seeds the default admin account if it doesn't already exist.
    """
    logger.info("Starting application - verifying database schema...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            logger.info("Default admin user not found - creating one...")
            admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                full_name="System Administrator",
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(admin)
            db.commit()
            logger.info("Default admin user created (username=admin / password=admin123)")
        else:
            logger.info("Default admin user already exists - skipping creation")
    finally:
        db.close()


# ==============================================================================
# AUTH ROUTES
# ==============================================================================
@app.post("/api/auth/login", response_model=Token, tags=["Auth"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Authenticate with username/password and receive a JWT access token."""
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    token = create_access_token({"user_id": user.id, "role": user.role.value})
    logger.info(f"User '{user.username}' logged in successfully")
    return Token(access_token=token)


@app.get("/api/auth/me", response_model=UserResponse, tags=["Auth"])
def read_current_user(current_user: User = Depends(get_current_user)):
    """Return the profile of the currently authenticated user."""
    return current_user


# ==============================================================================
# ADMIN ROUTES - USERS
# ==============================================================================
@app.post("/api/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED, tags=["Admin - Users"])
def create_user(payload: UserCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin creates a new user account."""
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        mobile_number=payload.mobile_number,
        address=payload.address,
        deposit=payload.deposit,
        monthly_rent=payload.monthly_rent,
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"Admin created new user '{user.username}' (id={user.id})")
    return user


@app.get("/api/users", response_model=PaginatedResponse[UserResponse], tags=["Admin - Users"])
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Admin lists all users (paginated)."""
    total = db.query(User).count()
    items = db.query(User).order_by(User.id).offset(skip).limit(limit).all()
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@app.get("/api/users/{user_id}", response_model=UserResponse, tags=["Admin - Users"])
def get_user(user_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin fetches a single user by id."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.put("/api/users/{user_id}", response_model=UserResponse, tags=["Admin - Users"])
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin updates an existing user's details (partial update)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "password" in update_data:
        user.password_hash = hash_password(update_data.pop("password"))
    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    logger.info(f"Admin updated user id={user_id}")
    return user


@app.delete("/api/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Admin - Users"])
def delete_user(user_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin deletes a user (cascades to their shop assignments)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    logger.info(f"Admin deleted user id={user_id}")
    return None


# ==============================================================================
# ADMIN ROUTES - SHOPS
# ==============================================================================
@app.post("/api/shops", response_model=ShopResponse, status_code=status.HTTP_201_CREATED, tags=["Admin - Shops"])
def create_shop(payload: ShopCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin creates a new shop."""
    if db.query(Shop).filter(Shop.shop_code == payload.shop_code).first():
        raise HTTPException(status_code=400, detail="Shop code already exists")
    if db.query(Shop).filter(Shop.sub_meter_number == payload.sub_meter_number).first():
        raise HTTPException(status_code=400, detail="Sub meter number already exists")

    shop = Shop(**payload.model_dump())
    db.add(shop)
    db.commit()
    db.refresh(shop)
    logger.info(f"Admin created shop '{shop.shop_name}' (id={shop.id})")
    return shop


@app.get("/api/shops", response_model=PaginatedResponse[ShopResponse], tags=["Admin - Shops"])
def list_shops(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Admin lists all shops (paginated)."""
    total = db.query(Shop).count()
    items = db.query(Shop).order_by(Shop.id).offset(skip).limit(limit).all()
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@app.get("/api/shops/{shop_id}", response_model=ShopResponse, tags=["Admin - Shops"])
def get_shop(shop_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin fetches a single shop by id."""
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    return shop


@app.put("/api/shops/{shop_id}", response_model=ShopResponse, tags=["Admin - Shops"])
def update_shop(shop_id: int, payload: ShopUpdate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin updates an existing shop's details (partial update)."""
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    update_data = payload.model_dump(exclude_unset=True)

    if "shop_code" in update_data and update_data["shop_code"] != shop.shop_code:
        if db.query(Shop).filter(Shop.shop_code == update_data["shop_code"]).first():
            raise HTTPException(status_code=400, detail="Shop code already exists")
    if "sub_meter_number" in update_data and update_data["sub_meter_number"] != shop.sub_meter_number:
        if db.query(Shop).filter(Shop.sub_meter_number == update_data["sub_meter_number"]).first():
            raise HTTPException(status_code=400, detail="Sub meter number already exists")

    for field, value in update_data.items():
        setattr(shop, field, value)

    db.commit()
    db.refresh(shop)
    logger.info(f"Admin updated shop id={shop_id}")
    return shop


@app.delete("/api/shops/{shop_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Admin - Shops"])
def delete_shop(shop_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin deletes a shop (cascades to its assignment record)."""
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    db.delete(shop)
    db.commit()
    logger.info(f"Admin deleted shop id={shop_id}")
    return None


# ==============================================================================
# ADMIN ROUTES - SHOP ASSIGNMENTS
# ==============================================================================
@app.post(
    "/api/shop-assignments",
    response_model=UserShopResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Admin - Assignments"],
)
def assign_shop(payload: ShopAssignmentCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin assigns a shop to a user. A shop may only be assigned to one user at a time."""
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role != UserRole.USER:
        raise HTTPException(status_code=400, detail="Shops can only be assigned to accounts with the 'user' role")

    shop = db.query(Shop).filter(Shop.id == payload.shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    existing = db.query(UserShop).filter(UserShop.shop_id == payload.shop_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="This shop is already assigned to a user")

    link = UserShop(user_id=payload.user_id, shop_id=payload.shop_id)
    db.add(link)
    db.commit()
    db.refresh(link)
    logger.info(f"Admin assigned shop id={shop.id} to user id={user.id}")
    return link


@app.delete("/api/shop-assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Admin - Assignments"])
def remove_assignment(assignment_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin removes a shop-to-user assignment."""
    link = db.query(UserShop).filter(UserShop.id == assignment_id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(link)
    db.commit()
    logger.info(f"Admin removed shop assignment id={assignment_id}")
    return None


# ==============================================================================
# ADMIN ROUTES - ELECTRICITY UNIT RATES
# ==============================================================================
@app.post("/api/unit-rates", response_model=RateResponse, status_code=status.HTTP_201_CREATED, tags=["Admin - Rates"])
def create_rate(payload: RateCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Admin sets a new per-unit electricity rate, effective from a given date."""
    rate = ElectricityRate(
        unit_rate=payload.unit_rate,
        effective_from=payload.effective_from,
        created_by=admin.id,
    )
    db.add(rate)
    db.commit()
    db.refresh(rate)
    logger.info(f"Admin set new unit rate: {rate.unit_rate} (effective {rate.effective_from})")
    return rate


@app.get("/api/unit-rates/current", response_model=RateResponse, tags=["Admin - Rates"])
def get_current_rate(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin fetches the currently active (most recent) unit rate."""
    rate = (
        db.query(ElectricityRate)
        .order_by(ElectricityRate.effective_from.desc(), ElectricityRate.id.desc())
        .first()
    )
    if not rate:
        raise HTTPException(status_code=404, detail="No unit rate configured yet")
    return rate


# ==============================================================================
# ADMIN ROUTES - DASHBOARD
# ==============================================================================
@app.get("/api/dashboard/admin", tags=["Admin - Dashboard"])
def admin_dashboard(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Admin dashboard: high-level counts and revenue summary across the system."""
    total_users = db.query(User).filter(User.role == UserRole.USER).count()
    total_shops = db.query(Shop).count()
    total_assigned_shops = db.query(UserShop).count()
    total_bills = db.query(Bill).count()
    pending_bills = db.query(Bill).filter(Bill.payment_status == PaymentStatus.PENDING).count()
    paid_bills = db.query(Bill).filter(Bill.payment_status == PaymentStatus.PAID).count()

    revenue_collected = (
        db.query(func.coalesce(func.sum(Bill.total_bill), 0.0))
        .filter(Bill.payment_status == PaymentStatus.PAID)
        .scalar()
    )
    revenue_pending = (
        db.query(func.coalesce(func.sum(Bill.total_bill), 0.0))
        .filter(Bill.payment_status == PaymentStatus.PENDING)
        .scalar()
    )

    return {
        "total_users": total_users,
        "total_shops": total_shops,
        "total_assigned_shops": total_assigned_shops,
        "total_bills": total_bills,
        "pending_bills": pending_bills,
        "paid_bills": paid_bills,
        "total_revenue_collected": round(revenue_collected or 0.0, 2),
        "total_revenue_pending": round(revenue_pending or 0.0, 2),
    }


# ==============================================================================
# USER ROUTES - MY SHOPS
# ==============================================================================
@app.get("/api/my-shops", response_model=List[ShopResponse], tags=["User - Shops"])
def my_shops(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Returns the list of shops assigned to the currently authenticated user."""
    shop_ids = [link.shop_id for link in db.query(UserShop).filter(UserShop.user_id == current_user.id).all()]
    if not shop_ids:
        return []
    return db.query(Shop).filter(Shop.id.in_(shop_ids)).all()


# ==============================================================================
# USER ROUTES - METER READINGS
# ==============================================================================
@app.post("/api/readings", response_model=ReadingResponse, status_code=status.HTTP_201_CREATED, tags=["User - Readings"])
def submit_reading(
    payload: ReadingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    User submits a monthly meter reading for one of their shops.
    The system automatically:
      1. Verifies the shop is assigned to the current user.
      2. Fetches the previous reading (most recent prior reading for the shop).
      3. Validates current_reading >= previous_reading.
      4. Calculates used_units = current_reading - previous_reading.
      5. Fetches the latest electricity unit rate.
      6. Fetches the shop owner's monthly rent.
      7. Calculates electricity_bill and total_bill.
      8. Saves both the meter reading and the generated bill.
    """
    # 1. Verify the shop belongs to the current user
    shop = _get_owned_shop_or_403(db, payload.shop_id, current_user)

    # 2. Only one reading per shop per month
    existing_reading = (
        db.query(MeterReading)
        .filter(
            MeterReading.shop_id == payload.shop_id,
            MeterReading.reading_year == payload.reading_year,
            MeterReading.reading_month == payload.reading_month,
        )
        .first()
    )
    if existing_reading:
        raise HTTPException(status_code=400, detail="A reading for this shop/month already exists")

    # 3. Fetch the previous reading automatically
    last_reading = (
        db.query(MeterReading)
        .filter(MeterReading.shop_id == payload.shop_id)
        .order_by(MeterReading.reading_year.desc(), MeterReading.reading_month.desc())
        .first()
    )
    if last_reading:
        previous_reading = last_reading.current_reading
    else:
        # First-ever reading for this shop: use the supplied starting value (or 0)
        previous_reading = payload.previous_reading if payload.previous_reading is not None else 0.0

    # 4. Current reading must never be less than previous reading
    if payload.current_reading < previous_reading:
        raise HTTPException(
            status_code=400,
            detail=f"Current reading ({payload.current_reading}) cannot be less than "
                   f"previous reading ({previous_reading})",
        )

    used_units = payload.current_reading - previous_reading

    # 5. Fetch the latest unit rate
    rate_row = (
        db.query(ElectricityRate)
        .order_by(ElectricityRate.effective_from.desc(), ElectricityRate.id.desc())
        .first()
    )
    if not rate_row:
        raise HTTPException(status_code=400, detail="No electricity unit rate has been configured yet")
    unit_rate = rate_row.unit_rate

    electricity_bill = round(used_units * unit_rate, 2)

    # 6. Fetch the rent for the shop's assigned (owning) user
    owner_link = db.query(UserShop).filter(UserShop.shop_id == payload.shop_id).first()
    owner = db.query(User).filter(User.id == owner_link.user_id).first()
    rent_amount = owner.monthly_rent

    total_bill = round(electricity_bill + rent_amount, 2)

    # 7. Save the meter reading
    reading = MeterReading(
        shop_id=payload.shop_id,
        reading_year=payload.reading_year,
        reading_month=payload.reading_month,
        previous_reading=previous_reading,
        current_reading=payload.current_reading,
        used_units=used_units,
        unit_rate=unit_rate,
        electricity_bill=electricity_bill,
        submitted_by=current_user.id,
    )
    db.add(reading)

    # 8. Save the auto-generated bill
    bill = Bill(
        user_id=owner.id,
        shop_id=payload.shop_id,
        bill_year=payload.reading_year,
        bill_month=payload.reading_month,
        rent_amount=rent_amount,
        used_units=used_units,
        unit_rate=unit_rate,
        electricity_bill=electricity_bill,
        total_bill=total_bill,
        payment_status=PaymentStatus.PENDING,
    )
    db.add(bill)

    db.commit()
    db.refresh(reading)
    logger.info(
        f"User '{current_user.username}' submitted reading for shop id={shop.id} "
        f"({payload.reading_month}/{payload.reading_year}) - bill auto-generated"
    )
    return reading


@app.get("/api/readings", response_model=List[ReadingResponse], tags=["User - Readings"])
def list_my_readings(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns meter readings for all shops owned by the current user."""
    shop_ids = [link.shop_id for link in db.query(UserShop).filter(UserShop.user_id == current_user.id).all()]
    if not shop_ids:
        return []
    return (
        db.query(MeterReading)
        .filter(MeterReading.shop_id.in_(shop_ids))
        .order_by(MeterReading.reading_year.desc(), MeterReading.reading_month.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


# ==============================================================================
# USER ROUTES - BILLS
# ==============================================================================
@app.get("/api/bills/my", response_model=List[BillResponse], tags=["User - Bills"])
def my_bills(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns all bills belonging to the current user."""
    return (
        db.query(Bill)
        .filter(Bill.user_id == current_user.id)
        .order_by(Bill.bill_year.desc(), Bill.bill_month.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@app.get("/api/bills/{bill_id}", response_model=BillResponse, tags=["User - Bills"])
def get_bill(bill_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Returns a single bill. Users may only view their own bills; admins may view any."""
    bill = db.query(Bill).filter(Bill.id == bill_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if current_user.role != UserRole.ADMIN and bill.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this bill")
    return bill


# ==============================================================================
# USER ROUTES - DASHBOARD
# ==============================================================================
@app.get("/api/dashboard/user", tags=["User - Dashboard"])
def user_dashboard(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """User dashboard: shop count, bill summary, and recent bills for the current user."""
    shop_count = db.query(UserShop).filter(UserShop.user_id == current_user.id).count()
    total_bills = db.query(Bill).filter(Bill.user_id == current_user.id).count()
    pending_bills = (
        db.query(Bill)
        .filter(Bill.user_id == current_user.id, Bill.payment_status == PaymentStatus.PENDING)
        .count()
    )
    pending_amount = (
        db.query(func.coalesce(func.sum(Bill.total_bill), 0.0))
        .filter(Bill.user_id == current_user.id, Bill.payment_status == PaymentStatus.PENDING)
        .scalar()
    )
    recent_bills = (
        db.query(Bill)
        .filter(Bill.user_id == current_user.id)
        .order_by(Bill.bill_year.desc(), Bill.bill_month.desc())
        .limit(5)
        .all()
    )

    return {
        "assigned_shops": shop_count,
        "total_bills": total_bills,
        "pending_bills": pending_bills,
        "pending_amount": round(pending_amount or 0.0, 2),
        "recent_bills": [BillResponse.model_validate(b) for b in recent_bills],
    }
