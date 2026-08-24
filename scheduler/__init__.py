"""
Cron-driven scheduled jobs for the Tenant Management System.

A package rather than loose scripts, so this folder's own `db_config.py` is
only ever reachable as `scheduler.db_config`. The entry scripts put the
PROJECT ROOT first on sys.path and import these modules by package path, so
the application's own `core.database` is what the app's modules keep getting -
this folder can never shadow it.
"""
