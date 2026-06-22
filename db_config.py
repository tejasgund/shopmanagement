"""
db_config.py - Database Configuration
Tenant Management System

Provides:
    - MySQL connection settings (via environment variables with defaults)
    - SQLAlchemy engine and session factory
    - Declarative Base for ORM models
    - Helper utilities: get_db dependency, test_connection
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError

from log import get_logger

logger = get_logger("app")

# ──────────────────────────────────────────────
# Database connection parameters
# Override any of these via environment variables.
# ──────────────────────────────────────────────
DB_HOST     = os.getenv("DB_HOST",     "localhost")
DB_PORT     = os.getenv("DB_PORT",     "3306")
DB_NAME     = os.getenv("DB_NAME",     "tenant_management")
DB_USER     = os.getenv("DB_USER",     "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root")

# ──────────────────────────────────────────────
# SQLAlchemy Database URL (MySQL + PyMySQL driver)
# pip install pymysql
# ──────────────────────────────────────────────
DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    f"?charset=utf8mb4"
)

# ──────────────────────────────────────────────
# Engine
# pool_pre_ping: validates connections before use (avoids stale-connection errors)
# pool_recycle:  recycle connections every 30 min (MySQL cuts idle conns at 8 h)
# echo:          set True to print raw SQL for debugging
# ──────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

# ──────────────────────────────────────────────
# Session factory
# autocommit=False, autoflush=False → explicit transaction control
# ──────────────────────────────────────────────
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)

# ──────────────────────────────────────────────
# Declarative Base – all ORM models inherit from this
# ──────────────────────────────────────────────
Base = declarative_base()


# ──────────────────────────────────────────────
# FastAPI dependency – yields a DB session per request
# ──────────────────────────────────────────────
def get_db():
    """
    FastAPI dependency that provides a database session.

    Usage:
        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    except Exception as exc:
        db.rollback()
        logger.error("Database error – rolling back transaction: %s", exc)
        raise
    finally:
        db.close()


# ──────────────────────────────────────────────
# Context-manager version (useful in scripts)
# ──────────────────────────────────────────────
@contextmanager
def get_db_context():
    """
    Context-manager wrapper around SessionLocal for use in plain scripts.

    Usage:
        with get_db_context() as db:
            db.query(User).all()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Database error – rolling back transaction: %s", exc)
        raise
    finally:
        db.close()


# ──────────────────────────────────────────────
# Connection health-check helper
# ──────────────────────────────────────────────
def test_connection() -> bool:
    """
    Attempt a lightweight query to verify the database is reachable.

    Returns:
        True if the connection succeeds, False otherwise.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection verified: %s@%s:%s/%s", DB_USER, DB_HOST, DB_PORT, DB_NAME)
        return True
    except OperationalError as exc:
        logger.error("Database connection FAILED: %s", exc)
        return False