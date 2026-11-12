"""
Tests for sync_schema()'s self-healing behaviour in models/schema.py.

These use the throwaway SQLite database from the `db` fixture (already
matching the model exactly, since `db` builds it via Base.metadata.create_all),
then fake the schema inspector to make one column look like it does on the
live production database today: razorpay_orders.bill_id is NOT NULL there,
even though the model has said nullable=True since whole-balance ("pay total
pending") payments were added. That mismatch is exactly what caused

    IntegrityError: (1048, "Column 'bill_id' cannot be null")

in production - sync_schema() only ever handled brand-new missing columns,
never a nullability change on a column that already existed. These tests
lock in the fix and the safety rule that goes with it: only ever relax
NOT NULL -> NULL automatically, never tighten the other way.
"""

from unittest.mock import MagicMock, patch

from models import schema as ct


def _fake_inspector_with_override(table_names, columns_by_table):
    fake_inspector = MagicMock()
    fake_inspector.get_table_names.return_value = table_names
    fake_inspector.get_columns.side_effect = lambda t: columns_by_table[t]
    fake_inspector.get_indexes.return_value = []
    fake_inspector.get_pk_constraint.return_value = {"constrained_columns": []}
    return fake_inspector


class _FakeConnection:
    """Records every ALTER TABLE statement sync_schema() would have run,
    without touching the real SQLite database (whose dialect doesn't even
    support MODIFY COLUMN, unlike the MySQL this script targets in production)."""

    def __init__(self):
        self.executed = []

    def execute(self, stmt):
        self.executed.append(str(stmt))

    def commit(self):
        pass

    def rollback(self):
        pass


def _snapshot_columns():
    """The `db` fixture already built every table from the current model, so
    this is the real, correct schema - a safe baseline to doctor one column on."""
    inspector = ct.sa_inspect(ct.engine)
    table_names = inspector.get_table_names()
    columns_by_table = {t: [dict(c) for c in inspector.get_columns(t)] for t in table_names}
    return table_names, columns_by_table


def test_sync_schema_relaxes_stale_not_null_column(db):
    table_names, columns_by_table = _snapshot_columns()
    for c in columns_by_table["razorpay_orders"]:
        if c["name"] == "bill_id":
            c["nullable"] = False  # simulate the live production mismatch

    conn = _FakeConnection()
    with patch("models.schema.sa_inspect") as mock_inspect:
        mock_inspect.return_value = _fake_inspector_with_override(table_names, columns_by_table)
        summary = ct.sync_schema(conn)

    assert "razorpay_orders.bill_id" in summary["columns_relaxed"]
    assert summary["columns_added"] == []  # nothing missing - only a nullability mismatch
    assert summary["errors"] == []

    relax_statements = [s for s in conn.executed if "razorpay_orders" in s and "MODIFY COLUMN" in s]
    assert len(relax_statements) == 1
    assert "`bill_id`" in relax_statements[0]
    assert "NULL" in relax_statements[0]
    assert "NOT NULL" not in relax_statements[0]


def test_sync_schema_never_tightens_nullable_to_not_null(db):
    """The reverse direction must never be attempted automatically - it could
    break existing rows that already hold NULL in that column."""
    table_names, columns_by_table = _snapshot_columns()
    for c in columns_by_table["razorpay_orders"]:
        if c["name"] == "user_id":  # NOT NULL on the model
            c["nullable"] = True    # but reported as already-nullable live

    conn = _FakeConnection()
    with patch("models.schema.sa_inspect") as mock_inspect:
        mock_inspect.return_value = _fake_inspector_with_override(table_names, columns_by_table)
        summary = ct.sync_schema(conn)

    assert "razorpay_orders.user_id" not in summary["columns_relaxed"]
    assert not any("user_id" in s and "MODIFY COLUMN" in s for s in conn.executed)


def test_sync_schema_is_a_no_op_when_schema_already_matches(db):
    """No mismatch anywhere -> nothing added, nothing relaxed, nothing altered."""
    table_names, columns_by_table = _snapshot_columns()

    conn = _FakeConnection()
    with patch("models.schema.sa_inspect") as mock_inspect:
        mock_inspect.return_value = _fake_inspector_with_override(table_names, columns_by_table)
        summary = ct.sync_schema(conn)

    assert summary["columns_added"] == []
    assert summary["columns_relaxed"] == []
    assert summary["errors"] == []
    assert not any("MODIFY COLUMN" in s or "ADD COLUMN" in s for s in conn.executed)
