"""
create_tables.py - Database Initialisation Script
Tenant Management System

Run once (or repeatedly – it is idempotent):
    python create_tables.py

What it does:
    1. Verifies the MySQL connection
    2. Creates ANY missing tables (brand-new models that don't exist yet in the DB)
    3. Adds ANY missing columns on tables that already exist but are missing
       fields defined on the ORM model (self-healing schema – safe to run repeatedly)
    4. Adds indexes and foreign keys (handled by SQLAlchemy metadata, best-effort
       for ones added after the table already exists)
    5. Inserts the default admin user (mobile: 8177809890)
    6. Prints a summary to stdout

Self-healing behaviour:
    Every time this script runs, it diffs Base.metadata against the live
    database schema:
      - A model with no matching table  -> CREATE TABLE (via create_all)
      - A model whose table exists but is missing one or more columns
        defined on the model -> ALTER TABLE ... ADD COLUMN for each missing
        column individually
    This means if you add a new Column(...) to any model below, or add an
    entirely new model class, simply re-running `python create_tables.py`
    brings the live database up to date without any manual migration.
"""

from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import or_

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
    user_shops       = relationship("UserShop",       back_populates="user", cascade="all, delete-orphan")
    bills            = relationship("Bill",           back_populates="user")
    audit_logs       = relationship("AuditLog",        back_populates="user")
    deposit_payments = relationship("DepositPayment", back_populates="user")


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

    id           = Column(Integer, primary_key=True, autoincrement=True)
    shop_number  = Column(String(50), nullable=False)
    area_sqft    = Column(Numeric(10, 2), nullable=True)
    status       = Column(Enum("available", "occupied", "maintenance", name="shop_status"),
                          nullable=False, default="available")
    complex_id   = Column(Integer, ForeignKey("complexes.id", ondelete="SET NULL"), nullable=True, index=True)
    # Default/standard monthly rent & deposit for this shop (used to prefill
    # UserShop.agreed_rent when a tenant is assigned, and as the fallback
    # rent figure wherever a tenant-specific agreed_rent has not been set).
    shop_rent    = Column(Numeric(10, 2), nullable=False, default=0)
    shop_deposit = Column(Numeric(10, 2), nullable=False, default=0)
    created_at   = Column(DateTime, nullable=False, default=now_utc)
    updated_at   = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    # Relationships
    complex          = relationship("Complex",        back_populates="shops")
    user_shops        = relationship("UserShop",       back_populates="shop", cascade="all, delete-orphan")
    bills             = relationship("Bill",           back_populates="shop")
    deposit_payments  = relationship("DepositPayment", back_populates="shop")


# ──────────────────────────────────────────────
# user_shops  (many-to-many junction)
# ──────────────────────────────────────────────
class UserShop(Base):
    __tablename__ = "user_shops"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id     = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    # Rent actually agreed with this tenant for this shop. Prefilled from
    # Shop.shop_rent at assignment time, but editable per-tenant so two
    # tenants in the same shop over time (or negotiated discounts) are
    # represented accurately. This is the figure the bill-creation "Rent"
    # flow should auto-fill from.
    agreed_rent = Column(Numeric(10, 2), nullable=True)
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
# deposit_payments
# ──────────────────────────────────────────────
class DepositPayment(Base):
    __tablename__ = "deposit_payments"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    shop_id      = Column(Integer, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount       = Column(Numeric(10, 2), nullable=False)
    payment_date = Column(DateTime, nullable=False, default=now_utc)
    remarks      = Column(Text, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=now_utc)

    # Relationships
    user = relationship("User", back_populates="deposit_payments")
    shop = relationship("Shop", back_populates="deposit_payments")


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
# SELF-HEALING SCHEMA SYNC
# Detects and fixes, on every run:
#   (a) entirely missing tables   -> CREATE TABLE
#   (b) missing columns on tables that DO exist -> ALTER TABLE ADD COLUMN
#   (c) missing indexes defined on the model     -> CREATE INDEX (best effort)
# Safe to run any number of times; every check is "if missing, then add".
# ══════════════════════════════════════════════════════════════════════════════

def _default_clause_for_column(column, dialect) -> str:
    """
    Build a safe SQL DEFAULT clause for an ALTER TABLE ADD COLUMN statement.
    Returns "" if there's no usable scalar default (caller then typically
    falls back to allowing NULL, or the column is required and the admin
    will need to backfill manually for pre-existing rows).
    """
    if column.default is None or not getattr(column.default, "is_scalar", False):
        return ""

    value = column.default.arg

    # Numeric-ish columns: no quotes
    if isinstance(value, (int, float, Decimal)):
        return f"DEFAULT {value}"
    # Booleans -> MySQL tinyint(1)
    if isinstance(value, bool):
        return f"DEFAULT {1 if value else 0}"
    # Callable defaults (e.g. now_utc) can't be expressed as a static SQL
    # DEFAULT for arbitrary dialects here — skip, NULL/NOT NULL governs instead.
    if callable(value):
        return ""
    # Fall back to a quoted string literal
    escaped = str(value).replace("'", "''")
    return f"DEFAULT '{escaped}'"


def sync_schema(connection) -> dict:
    """
    Compare Base.metadata against the live database and heal any gaps:
      - Missing tables  -> created via Base.metadata.create_all (checkfirst)
      - Missing columns on existing tables -> ALTER TABLE ADD COLUMN
      - Missing simple/unique indexes -> CREATE INDEX (best effort, ignored
        if the index engine-specific syntax doesn't apply)

    Returns a summary dict: {"tables_created": [...], "columns_added": [...],
    "indexes_added": [...], "errors": [...]}
    """
    summary = {"tables_created": [], "columns_added": [], "indexes_added": [], "errors": []}

    inspector = sa_inspect(engine)
    existing_tables_before = set(inspector.get_table_names())

    # ── Step 1: create any tables that don't exist at all ──
    all_model_tables = set(Base.metadata.tables.keys())
    missing_tables = all_model_tables - existing_tables_before
    if missing_tables:
        Base.metadata.create_all(bind=engine, checkfirst=True)
        summary["tables_created"] = sorted(missing_tables)
        for t in sorted(missing_tables):
            print(f"  ✔  Created missing table: {t}")
            logger.info("Created missing table: %s", t)

    # Refresh inspector state after potential table creation
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # ── Step 2: for tables that exist (pre-existing or just created), check columns ──
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            # Should not happen post create_all, but guard anyway
            continue
        if table.name in missing_tables:
            # Brand new table already has every column from create_all
            continue

        existing_cols = {col["name"] for col in inspector.get_columns(table.name)}

        for column in table.columns:
            if column.name in existing_cols:
                continue  # column already present – nothing to heal

            col_type = column.type.compile(dialect=engine.dialect)
            default_clause = _default_clause_for_column(column, engine.dialect)

            # A NOT NULL column being added to a table that may already have
            # rows needs a default, otherwise the ALTER fails on most engines.
            # If the model declares NOT NULL but we couldn't derive a literal
            # default (e.g. callable like now_utc), relax to NULLable so the
            # migration doesn't break existing data; the app should backfill.
            if column.nullable:
                nullable_clause = "NULL"
            elif default_clause:
                nullable_clause = "NOT NULL"
            else:
                nullable_clause = "NULL"
                logger.warning(
                    "Column %s.%s is NOT NULL with no static default; "
                    "adding as NULLable to avoid breaking existing rows.",
                    table.name, column.name,
                )

            alter_sql = (
                f"ALTER TABLE `{table.name}` "
                f"ADD COLUMN `{column.name}` {col_type} {nullable_clause} {default_clause};"
            )
            try:
                connection.execute(text(alter_sql))
                connection.commit()
                summary["columns_added"].append(f"{table.name}.{column.name}")
                print(f"  ✔  Added missing column: {table.name}.{column.name}")
                logger.info("Added missing column: %s.%s", table.name, column.name)
            except Exception as exc:
                connection.rollback()
                summary["errors"].append(f"{table.name}.{column.name}: {exc}")
                logger.warning("Could not add column %s.%s: %s", table.name, column.name, exc)

        # ── Step 3: best-effort check for missing simple/unique indexes ──
        try:
            existing_index_cols = set()
            for idx in inspector.get_indexes(table.name):
                existing_index_cols.add(tuple(idx["column_names"]))
            # Also treat existing unique constraints / PKs as covering
            pk_cols = tuple(inspector.get_pk_constraint(table.name).get("constrained_columns") or [])
            if pk_cols:
                existing_index_cols.add(pk_cols)

            for index in table.indexes:
                idx_cols = tuple(col.name for col in index.columns)
                if idx_cols in existing_index_cols:
                    continue
                # Skip if any column in the index didn't exist before this run
                # and failed to be added above
                if not all(c in {col["name"] for col in inspector.get_columns(table.name)} for c in idx_cols):
                    continue
                unique_kw = "UNIQUE " if index.unique else ""
                cols_sql = ", ".join(f"`{c}`" for c in idx_cols)
                create_idx_sql = f"CREATE {unique_kw}INDEX `{index.name}` ON `{table.name}` ({cols_sql});"
                try:
                    connection.execute(text(create_idx_sql))
                    connection.commit()
                    summary["indexes_added"].append(f"{table.name}:{index.name}")
                    print(f"  ✔  Added missing index: {table.name}.{index.name}")
                    logger.info("Added missing index: %s.%s", table.name, index.name)
                except Exception as exc:
                    connection.rollback()
                    logger.warning("Could not add index %s.%s: %s", table.name, index.name, exc)
        except Exception as exc:
            logger.warning("Index check skipped for %s: %s", table.name, exc)

    return summary


# Backwards-compatible alias (older scripts/imports may reference this name)
def add_missing_columns(connection):
    return sync_schema(connection)


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

    # 2. Self-healing schema sync:
    #    - creates any tables missing entirely
    #    - adds any columns missing from existing tables
    #    - adds any indexes missing from existing tables
    print("Checking schema …")
    with engine.connect() as conn:
        summary = sync_schema(conn)

    if not any([summary["tables_created"], summary["columns_added"], summary["indexes_added"]]):
        print("✔  Schema already up to date – nothing to change.\n")
    else:
        print()
        if summary["tables_created"]:
            print(f"✔  Tables created: {len(summary['tables_created'])} -> {summary['tables_created']}")
        if summary["columns_added"]:
            print(f"✔  Columns added:  {len(summary['columns_added'])} -> {summary['columns_added']}")
        if summary["indexes_added"]:
            print(f"✔  Indexes added:  {len(summary['indexes_added'])} -> {summary['indexes_added']}")
        print()
    if summary["errors"]:
        print(f"⚠  {len(summary['errors'])} schema change(s) could not be applied automatically:")
        for err in summary["errors"]:
            print(f"   - {err}")
        print()

    # 3. Seed default admin user
    db = SessionLocal()
    try:
        DEFAULT_MOBILE   = "8177809890"
        DEFAULT_PASSWORD = "Sujata8@Tekale8@"
        DEFAULT_NAME     = "Tejas Gund"

#        existing = db.query(User).filter(User.mobile == DEFAULT_MOBILE).first()
        existing = (
            db.query(User)
            .filter(
                or_(
                    User.email == "admin@tenantapp.com",
                    User.mobile == DEFAULT_MOBILE,
                )
            )
            .first()
        )
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