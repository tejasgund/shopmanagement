"""
Cron-driven scheduled jobs for the Tenant Management System.

A package rather than loose scripts so that `scheduler/db_config.py` can sit
next to the app's own `db_config.py` without either shadowing the other:
run_scheduler.py puts the PROJECT ROOT first on sys.path and reaches these
modules by package path, so a bare `import db_config` from inside the
application (create_tables.py does exactly that) still resolves to the
application's one.
"""
