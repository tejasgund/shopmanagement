"""
Individual scheduler tasks.

One module per task, each exposing `run(db, run_date, cfg) -> dict`. They are
thin: the actual rules live in scheduler/billing/, so the same logic backs
the nightly run and the admin's manual trigger. The master
scheduler never imports these directly - scheduler.service resolves them by
name at run time, which is why adding a task is one registry entry and no
edits anywhere else.

The returned dict is stored against the task row and shown on the dashboard;
`records_processed` and `records_failed` are read from it if present.
"""
