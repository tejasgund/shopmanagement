"""
================================================================================
 createMN.py
================================================================================
 Stand-alone database initialization script for the Shop Electricity Bill
 Management System.

 Run this BEFORE starting the API server. It connects to MySQL (through the
 same pool/engine used by main.py) and creates every table that does not
 already exist. SQLAlchemy's Base.metadata.create_all() is idempotent and
 safe to re-run at any time - it will NOT touch tables/columns that already
 exist, it only creates what's missing.

 Usage:
     python createMN.py
================================================================================
"""

# Re-use the exact same Base (table definitions) and engine (DB connection)
# that main.py already builds, so the two files always stay in sync.
from main import Base, engine
from log import get_logger

logger = get_logger("createMN")


def init_database() -> None:
    """Creates every table declared on Base.metadata if it doesn't already exist."""
    logger.info("Connecting to database and verifying schema...")
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.error(f"Failed to create/verify tables: {exc}")
        raise

    table_names = list(Base.metadata.tables.keys())
    logger.info(f"Schema verified. Tables managed: {table_names}")
    logger.info("Database initialization complete. You can now run: uvicorn main:app --reload")


if __name__ == "__main__":
    init_database()
