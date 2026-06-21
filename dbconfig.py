"""
================================================================================
 dbconfig.py
================================================================================
 MySQL connection pool configuration (mysql-connector-python).

 This pool is the single source of physical database connections for the
 whole application. SQLAlchemy is configured (in main.py) to pull its
 connections from this exact pool via the `creator` parameter, so all ORM
 queries ultimately flow through `get_connection()` below.

 NOTE: Update host/user/password/database for your environment before
 running the application.
================================================================================
"""
from mysql.connector import pooling

# Connection pool configuration
db_pool = pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=10,
    host="172.31.52.221",
    user="admin",
    password="admin",
    database="test_lightbill"
)


def get_connection():
    """Return a raw (DBAPI) connection from the pool."""
    return db_pool.get_connection()
