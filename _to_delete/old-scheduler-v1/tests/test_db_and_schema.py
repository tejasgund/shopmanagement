"""
Startup behaviour: the scheduler creates what it owns, never what it does not,
and says something useful when the application's schema moves underneath it.
"""

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

from scheduler import models


def test_ensure_schema_creates_the_scheduler_table_on_a_fresh_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'fresh.db'}", future=True)
    created = models.ensure_schema(engine)
    assert created == ["scheduler_tasks"]
    assert inspect(engine).has_table("scheduler_tasks")
    # Second call is a no-op - every run calls it.
    assert models.ensure_schema(engine) == []
    engine.dispose()


def test_ensure_schema_never_creates_the_applications_tables(tmp_path):
    """A scheduler pointed at the wrong database must not quietly invent an
    empty `bills` and start billing nobody."""
    engine = create_engine(f"sqlite:///{tmp_path/'fresh.db'}", future=True)
    models.ensure_schema(engine)
    inspector = inspect(engine)
    for table in ("bills", "users", "shops", "app_settings", "audit_logs"):
        assert not inspector.has_table(table), f"the scheduler created {table}"
    engine.dispose()


def test_verify_schema_reports_a_missing_table_it_writes_to(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'fresh.db'}", future=True)
    models.ensure_schema(engine)
    problems = models.verify_schema(engine)
    assert any("bills" in p for p in problems)
    engine.dispose()


def test_verify_schema_is_quiet_when_the_schema_matches(engine):
    assert models.verify_schema(engine) == []


def test_verify_schema_flags_a_new_required_column_it_does_not_set(tmp_path):
    """
    The one upstream change that turns an INSERT here into an integrity error:
    a NOT NULL column with no default, added by the app, unknown to this
    process. Better a clear warning at 02:00 than an opaque driver error.
    """
    path = tmp_path / "drifted.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE bills ADD COLUMN late_fee_code VARCHAR(10) NOT NULL")

    problems = models.verify_schema(engine)
    assert any("late_fee_code" in p for p in problems)
    engine.dispose()
