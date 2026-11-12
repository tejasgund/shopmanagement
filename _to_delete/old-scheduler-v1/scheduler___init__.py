"""
The scheduler: every scheduled job for the Tenant Management System, run by cron.

Self-contained on purpose. Nothing in this package imports the application -
not its configuration, not its logging, not its models, not its services. It
has its own scheduler.conf, its own database access, its own ORM mapping, its
own settings reader, its own per-task logging and its own tests, so it can be
installed, run, tested and restarted on a machine where the application's
source is not present.

    config.py          scheduler.conf + environment: database, timezone, logging
    db.py              engine/session, plus the startup schema checks
    models.py          its own ORM mapping (see that file on owned vs mirrored)
    settings.py        the scheduler.* switches, read from the shared database
    logging_setup.py   one log file per task
    errors.py          error vocabulary and the exit codes every script uses
    money.py           what a bill owes
    billing/           the rules: rent generation, late-payment penalty
    service.py         the ledger: task registry, statuses, due/missed queries
    master.py          the coordinator: which task gets a turn, and recording it
    tasks/             one thin module per task
    run_*.py           the cron entry points, one per task plus the master sweep

The dependency between the two services runs one way only: the APPLICATION
imports this package (for the manual "Generate rent now" button, the penalty
explanation shown to tenants, and the Scheduler settings screen). This package
imports nothing from the application. The database is the only thing they
share.
"""
