"""
scheduler/errors.py - the scheduler's own error vocabulary and exit codes.

Three rules, in one place because they are easy to get subtly inconsistent
when spread across four run scripts:

  1. A failing TASK is not a failing RUN. The task is recorded as FAILED with
     its traceback and is retryable from the dashboard; the process still
     exits 0, because cron mailing about something the dashboard already owns
     is noise. Only an error that escapes the scheduler itself - it could not
     reach the database, the config is unusable - is a failed run.

  2. Every exception is recorded before it is re-raised or swallowed. A
     traceback that only reached stderr is a traceback cron may have thrown
     away.

  3. Failures are isolated per task. One task blowing up must never cost the
     others their turn.
"""

import traceback

# ── Process exit codes ────────────────────────────────────────────────────
# Kept identical across every run script so one monitoring rule covers them all.
EXIT_OK = 0            # ran, including "ran and had nothing to do"
EXIT_RUN_FAILED = 1    # the run itself failed - see the log for the traceback
EXIT_SKIPPED = 2       # deliberately did nothing: switched off in settings
EXIT_BAD_USAGE = 3     # unknown job name, unparseable date


class SchedulerError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(SchedulerError):
    """scheduler.conf (or the environment) does not describe a usable setup."""


class DatabaseUnavailable(SchedulerError):
    """The database could not be reached, or is missing the scheduler's table."""


class TaskBusy(SchedulerError):
    """Another process holds this task's lock; its run covers the same work."""


def describe(exc: BaseException) -> str:
    """One line: the exception type and its message."""
    return f"{type(exc).__name__}: {exc}".strip()


def detail(exc: BaseException, limit: int = 4000) -> str:
    """
    The line above plus the traceback, truncated to fit the ledger column.

    Truncated at the END rather than the start: the first frames say what was
    being attempted, which is what someone reads first.
    """
    text = describe(exc) + "\n\n" + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    return text[:limit]


def log_exception(logger, message: str, exc: BaseException) -> None:
    """Record a failure in this task's own log file, traceback included."""
    logger.error("%s | %s", message, describe(exc))
    logger.error("%s", "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).rstrip())
