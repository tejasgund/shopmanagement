"""
One log file per task.

The rules these lock down: after a night's run you can open exactly one file
to find out what one task did, without another task's output interleaved into
it, and one more file to find out whether anything broke at all.
"""

import os

from scheduler import config, logging_setup, master
from scheduler import service as svc
from .conftest import set_settings


def _register_today(db, *task_names):
    today = svc.now_local().date()
    for name in task_names:
        svc.register(db, name, svc.occurrence_at(name, today), run_date=today)
    db.commit()
    return today


def test_each_task_writes_only_its_own_file(db, logs, tenant, shop):
    """A sweep that runs several tasks must not mix their output together."""
    set_settings(db, {
        "scheduler.enabled": True,
        "scheduler.rent_generation_enabled": True,
        "scheduler.penalty_enabled": True,
    })
    _register_today(db, "rent_generation", "due_date_penalty")
    master.run_due_tasks(db)

    rent_log = logs.read("rent_generation")
    penalty_log = logs.read("due_date_penalty")

    assert "rent_generation" in rent_log
    assert "due_date_penalty" not in rent_log
    assert "due_date_penalty" in penalty_log
    assert "rent_generation" not in penalty_log


def test_the_coordinator_has_its_own_file(db, logs, tenant, shop):
    set_settings(db, {"scheduler.enabled": True})
    master.run_due_tasks(db)

    assert os.path.exists(logs.path("master"))
    assert "future_task_checker" in logs.read("master")


def test_a_failing_task_writes_its_traceback_to_its_own_file_and_to_errors_log(
    db, logs, tenant, shop, monkeypatch,
):
    """
    A failure has to be findable two ways: in the task's own file (what
    happened to THIS task) and in errors.log (did anything break tonight).
    """
    import scheduler.tasks.rent_generation as rent_task

    def boom(*args, **kwargs):
        raise RuntimeError("rent generation exploded")

    monkeypatch.setattr(rent_task, "run", boom)
    set_settings(db, {"scheduler.enabled": True, "scheduler.rent_generation_enabled": True})
    _register_today(db, "rent_generation")

    master.run_due_tasks(db, only_task="rent_generation")

    own = logs.read("rent_generation")
    shared = logs.read("errors")

    assert "rent generation exploded" in own
    assert "Traceback" in own
    assert "rent generation exploded" in shared

    # Another task's file is untouched by this one's failure.
    assert "exploded" not in logs.read("due_date_penalty")


def test_the_shared_error_log_carries_nothing_but_failures(db, logs, tenant, shop):
    set_settings(db, {"scheduler.enabled": True,
                      "scheduler.rent_generation_enabled": True})
    _register_today(db, "rent_generation")
    master.run_due_tasks(db, only_task="rent_generation")

    assert "Finished rent_generation" in logs.read("rent_generation")
    assert "Finished rent_generation" not in logs.read("errors")


def test_log_files_land_in_the_configured_directory(db, logs):
    """SCHEDULER_LOG_DIR / [logging] dir is honoured, not hardcoded."""
    logger = logging_setup.get_logger("rent_generation")
    logger.info("hello from the test")

    assert logging_setup.log_path("rent_generation").startswith(config.log_dir())
    assert "hello from the test" in logs.read("rent_generation")
