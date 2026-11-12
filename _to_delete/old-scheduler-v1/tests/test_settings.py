"""
The scheduler reads its switches from the database, and defaults sanely when
it cannot.
"""

import pytest

from scheduler import settings as scheduler_settings
from .conftest import set_settings


def test_defaults_apply_when_nothing_is_customised(db):
    cfg = scheduler_settings.get_all(db)
    assert cfg["scheduler.enabled"] is True
    assert cfg["scheduler.penalty_enabled"] is False      # opt-in, never on by upgrade
    assert cfg["scheduler.lookahead_days"] == 7


def test_stored_values_override_defaults(db):
    set_settings(db, {"scheduler.penalty_enabled": True,
                      "scheduler.penalty_percent_per_day": 2.5})
    cfg = scheduler_settings.get_all(db)
    assert cfg["scheduler.penalty_enabled"] is True
    assert cfg["scheduler.penalty_percent_per_day"] == 2.5


def test_an_unusable_stored_value_falls_back_rather_than_failing_the_run(db):
    """A scheduler that refuses to start over one bad value is worse than one
    that runs with the documented default and says so."""
    set_settings(db, {"scheduler.lookahead_days": "not-a-number"})
    cfg = scheduler_settings.get_all(db)
    assert cfg["scheduler.lookahead_days"] == 7


def test_unknown_keys_are_rejected(db):
    with pytest.raises(KeyError):
        scheduler_settings.get(db, "scheduler.does_not_exist")


def test_validation_rejects_values_that_would_make_a_run_absurd():
    assert scheduler_settings.validate({"scheduler.penalty_percent_per_day": 500})
    assert scheduler_settings.validate({"scheduler.penalty_grace_days": -1})
    assert scheduler_settings.validate({"scheduler.penalty_max_amount": -5})
    assert scheduler_settings.validate({"scheduler.backfill_days": 5000})
    assert scheduler_settings.validate({"scheduler.lookahead_days": 400})
    assert scheduler_settings.validate({"scheduler.penalty_percent_per_day": 1.5}) is None


def test_describe_only_exposes_keys_the_scheduler_owns(db):
    keys = {item["key"] for item in scheduler_settings.describe()}
    assert keys == set(scheduler_settings.SCHEDULER_DEFAULTS)
    # bill.due_days is READ here but owned by the app - it must not appear on
    # the Scheduler settings screen.
    assert "bill.due_days" not in keys
    assert "bill.due_days" in scheduler_settings.DEFAULTS
