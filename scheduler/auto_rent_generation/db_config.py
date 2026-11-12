"""
Database connection for the Auto Rent Generation scheduler.

Fill in the five values below with your real database details, then:

    chmod 600 db_config.py

This file holds a password. Keep it readable only by the user cron runs as.

Nothing else reads this file. due_bill_penalty has its own db_config.py with
its own values, and the two never import each other - so this scheduler can be
pointed at a different host, a replica, or a different credential without the
penalty scheduler noticing.

In practice both usually hold the SAME credentials, because both schedulers
work on the same tenant-management database. Two files, one database, and the
freedom to change that later.
"""

DB_CONFIG = {
    "host":     "YOUR_DB_HOST",        # e.g. "172.31.52.221" or "localhost"
    "port":     3306,                  # MySQL / MariaDB default
    "database": "YOUR_DATABASE_NAME",  # e.g. "tenant_management"
    "user":     "YOUR_DATABASE_USER",
    "password": "YOUR_DATABASE_PASSWORD",
}
