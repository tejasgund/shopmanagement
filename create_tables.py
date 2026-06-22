"""
create_tables.py - Database Initialisation Script
Tenant Management System

Run once (or repeatedly – it is idempotent):
    python create_tables.py

What it does:
    1. Verifies the MySQL connection
    2. Creates all tables if they do not yet exist
    3. Adds indexes and foreign keys (handled by SQLAlchemy metadata)
    4. Inserts the default admin user (mobile: 9999999999 / admin@123)
    5. Prints a summary to stdout
"""

from datetime import datetime, timezone

import bcrypt
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime,
    Boolean, ForeignKey, Enum, Index, inspect as sa_inspect,
    text,
)
from sqlalchemy.orm import relationship

from db_config import Base, engine, SessionLocal, test_connection
from log import get_logger

logger = get_logger("app")

# ══════════════════════════════════════════════════════════════════════════════
# ORM MODELS
# These are imported by app.py as well – define them here so create_tables.py
# is the single source of truth for schema.
# ══════════════════════════════════════════════════════════════════════════════


def now_utc():
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────
# users
# ──────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    name          = Column(String(120), nullable=False)
    mobile        = Column(String(15),  nullable=False, unique=True, index=True)
    email         = Column(String(150), nullable=True,  unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role          = Column(Enum("admin", "tenant", name="user_role"), nullable=False, default="tenant")
    is_active     = Column(Boolean, nullable=False, default=True)
    created_at    = Column(DateTime, nullable=False, default=now_utc)
    updated_at    = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    user_shops = relationship("UserShop",   back_populates="user", cascade="all, delete-orphan")
    bills      = relationship("Bill",       back_populates="user")
    audit_logs = relationship("AuditLog",   back_populates="user")


# ──────────────────────────────────────────────
# complexes
# ──────────────────────────────────────────────
class Complex(Base):
    __tablename__ = "complexes"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(150), nullable=False)
    address     = Column(Text,        nullable=True)
    description = Column(Text,        nullable=True)
    created_at  = Column(DateTime, nullable=False, default=now_utc)
    updated_at  = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    shops = relationship("Shop", back_populates="complex")


# ──────────────────────────────────────────────
# shops
# ──────────────────────────────────────────────
class Shop(Base):
    __tablename__ = "shops"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    shop_number = Column(String(50), nullable=False)
    area_sqft   = Column(Numeric(10, 2), nullable=True)
    status      = Column(Enum("available", "occupied", "maintenance", name="shop_status"),
                         nullable=False, default="available")
    complex_id  = Column(Integer, ForeignKey("complexes.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at  = Column(DateTime, nullable=False, default=now_utc)
    updated_at  = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    complex    = relationship("Complex",  back_populates="shops")
    user_shops = relationship("UserShop", back_populates="shop", cascade="all, delete-orphan")
    bills      = relationship("Bill",     back_populates="shop")


# ──────────────────────────────────────────────
# user_shops  (many-to-many junction)
# ──────────────────────────────────────────────
class UserShop(Base):
    __tablename__ = "user_shops"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id     = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    assigned_at = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    user = relationship("User", back_populates="user_shops")
    shop = relationship("Shop", back_populates="user_shops")

    # A shop can be assigned to a user only once
    __table_args__ = (
        Index("uq_user_shop", "user_id", "shop_id", unique=True),
    )


# ──────────────────────────────────────────────
# bills
# ──────────────────────────────────────────────
class Bill(Base):
    __tablename__ = "bills"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    shop_id         = Column(Integer, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False, index=True)
    bill_type       = Column(String(80),  nullable=False)
    description     = Column(Text,        nullable=True)
    amount          = Column(Numeric(12, 2), nullable=False)
    paid_amount     = Column(Numeric(12, 2), nullable=False, default=0)
    pending_amount  = Column(Numeric(12, 2), nullable=False, default=0)
    bill_date       = Column(DateTime, nullable=False, default=now_utc)
    due_date        = Column(DateTime, nullable=True)
    status          = Column(Enum("pending", "partial", "paid", name="bill_status"),
                             nullable=False, default="pending")
    created_at      = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    user     = relationship("User",    back_populates="bills")
    shop     = relationship("Shop",    back_populates="bills")
    payments = relationship("Payment", back_populates="bill", cascade="all, delete-orphan")


# ──────────────────────────────────────────────
# payments
# ──────────────────────────────────────────────
class Payment(Base):
    __tablename__ = "payments"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    bill_id        = Column(Integer, ForeignKey("bills.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount         = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(60), nullable=False)
    payment_date   = Column(DateTime, nullable=False, default=now_utc)
    remarks        = Column(Text, nullable=True)
    created_at     = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    bill = relationship("Bill", back_populates="payments")


# ──────────────────────────────────────────────
# audit_logs
# ──────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action     = Column(String(50),  nullable=False)          # CREATE / UPDATE / DELETE / LOGIN
    table_name = Column(String(80),  nullable=True)
    record_id  = Column(Integer,     nullable=True)
    old_data   = Column(Text,        nullable=True)           # JSON string
    new_data   = Column(Text,        nullable=True)           # JSON string
    created_at = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    user = relationship("User", back_populates="audit_logs")


# ══════════════════════════════════════════════════════════════════════════════
# HELPER – hash a plaintext password
# ══════════════════════════════════════════════════════════════════════════════

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# ══════════════════════════════════════════════════════════════════════════════
# HELPER – add missing columns to an existing table (basic ALTER TABLE support)
# ══════════════════════════════════════════════════════════════════════════════

def add_missing_columns(connection):
    """
    Inspect each mapped table and ALTER TABLE … ADD COLUMN for any column that
    exists in the ORM model but is absent from the live database schema.
    Skips columns that already exist – safe to run on every startup.
    """
    inspector = sa_inspect(engine)
    existing_tables = inspector.get_table_names()

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table – CREATE TABLE will handle it

        existing_cols = {col["name"] for col in inspector.get_columns(table.name)}

        for column in table.columns:
            if column.name in existing_cols:
                continue

            # Build a minimal ALTER TABLE statement
            col_type = column.type.compile(dialect=engine.dialect)
            nullable = "NULL" if column.nullable else "NOT NULL"
            default_clause = ""
            if column.default is not None and column.default.is_scalar:
                default_clause = f"DEFAULT '{column.default.arg}'"

            alter_sql = (
                f"ALTER TABLE `{table.name}` "
                f"ADD COLUMN `{column.name}` {col_type} {nullable} {default_clause};"
            )
            try:
                connection.execute(text(alter_sql))
                connection.commit()
                print(f"  ✔  Added missing column: {table.name}.{column.name}")
                logger.info("Added missing column: %s.%s", table.name, column.name)
            except Exception as exc:
                logger.warning("Could not add column %s.%s: %s", table.name, column.name, exc)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "═" * 50)
    print("  Tenant Management System – Database Setup")
    print("═" * 50 + "\n")

    # 1. Verify connectivity
    if not test_connection():
        print("❌  Cannot connect to the database. Check db_config.py / environment variables.")
        raise SystemExit(1)
    print("✔  Database Connected\n")

    # 2. Create tables (CREATE TABLE IF NOT EXISTS for all mapped models)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("✔  Tables Created Successfully\n")

    # 3. Add any columns that exist in models but not in DB (schema migration lite)
    with engine.connect() as conn:
        add_missing_columns(conn)

    # 4. Seed default admin user
    db = SessionLocal()
    try:
        DEFAULT_MOBILE   = "9999999999"
        DEFAULT_PASSWORD = "admin@123"
        DEFAULT_NAME     = "Super Admin"

        existing = db.query(User).filter(User.mobile == DEFAULT_MOBILE).first()
        if existing:
            print("ℹ  Default admin already exists – skipping seed.\n")
        else:
            admin = User(
                name          = DEFAULT_NAME,
                mobile        = DEFAULT_MOBILE,
                email         = "admin@tenantapp.com",
                password_hash = hash_password(DEFAULT_PASSWORD),
                role          = "admin",
                is_active     = True,
            )
            db.add(admin)
            db.commit()
            print("✔  Default Admin Created")
            print(f"   Mobile  : {DEFAULT_MOBILE}")
            print(f"   Password: {DEFAULT_PASSWORD}")
            print(f"   Role    : admin\n")

            # Audit the seed action
            log_entry = AuditLog(
                user_id    = admin.id,
                action     = "CREATE",
                table_name = "users",
                record_id  = admin.id,
                old_data   = None,
                new_data   = f'{{"mobile":"{DEFAULT_MOBILE}","role":"admin"}}',
            )
            db.add(log_entry)
            db.commit()

    except Exception as exc:
        db.rollback()
        logger.error("Seed failed: %s", exc)
        print(f"❌  Seed error: {exc}")
        raise
    finally:
        db.close()

    print("═" * 50)
    print("  Setup complete. You can now start the API server.")
    print("  Run: uvicorn app:app --reload")
    print("═" * 50 + "\n")


if __name__ == "__main__":
    main()