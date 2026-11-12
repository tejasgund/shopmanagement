"""
scheduler/logging_setup.py - one log file per task.

Every task writes to its own file, so "what happened to the penalty run last
night" is answered by opening one file rather than grepping a shared log for
lines that belong to it:

    logs/rent_generation.log       everything the Rent Generation task did
    logs/due_date_penalty.log      everything the Due Date Penalty task did
    logs/future_task_checker.log   everything the Future Task Checker did
    logs/master.log                the coordinator: which tasks got a turn
    logs/errors.log                ERROR and above from ALL of the above

errors.log is the exception to one-file-per-task on purpose. Per-task files
answer "what did this task do"; a single error file answers "did anything
break last night", which is the question monitoring actually asks. Nothing is
only in errors.log - every line there is also in its own task's file.

Files rotate at midnight and old ones are gzipped, so a task that logs every
day does not grow without limit. Retention is configurable in scheduler.conf.

This is the scheduler's own logging, not the application's. It writes into
the scheduler's own folder by default, which matters when cron runs as a
different user than the API: a scheduler writing into the app's log directory
is how you end up with a root-owned app.log the API can no longer rotate.
"""

import gzip
import logging
import os
import shutil
from logging.handlers import TimedRotatingFileHandler

from scheduler import config

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ERROR_LOG_NAME = "errors"

# Loggers already built in this process. A run script and the coordinator can
# both ask for the same task's logger; handing back the same object keeps one
# line from being written twice.
_loggers: dict = {}


def ensure_log_dir() -> str:
    directory = config.log_dir()
    os.makedirs(directory, exist_ok=True)
    return directory


def _gzip_rotator(source: str, dest: str) -> None:
    """Compress yesterday's file once it has been rotated out."""
    with open(source, "rb") as f_in, gzip.open(dest + ".gz", "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)


def _file_handler(path: str, level: int) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=config.log_retention_days(),
        encoding="utf-8",
        utc=False,
        delay=True,      # do not create the file until something is logged
    )
    handler.rotator = _gzip_rotator
    handler.namer = lambda name: name          # .gz appended by the rotator
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


def get_logger(name: str) -> logging.Logger:
    """
    The logger for one task (or "master"). Safe to call repeatedly.

    `name` becomes the file name, so it must be a task name from the registry
    or one of the fixed names above - not free text.
    """
    if name in _loggers:
        return _loggers[name]

    directory = ensure_log_dir()
    logger = logging.getLogger(f"scheduler.{name}")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, config.log_level(), logging.INFO))

    # This task's own file: everything at the configured level.
    logger.addHandler(_file_handler(os.path.join(directory, f"{name}.log"),
                                    logger.level))

    # The shared failure file: errors only, from every task.
    logger.addHandler(_file_handler(os.path.join(directory, f"{ERROR_LOG_NAME}.log"),
                                    logging.ERROR))

    if config.log_to_console():
        console = logging.StreamHandler()
        console.setLevel(logger.level)
        console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        logger.addHandler(console)

    # Nothing above this in the hierarchy is configured by the scheduler;
    # propagating would hand these records to whatever else is in the process
    # (a test runner, the API, if the scheduler is ever imported there) and
    # print each line twice.
    logger.propagate = False

    _loggers[name] = logger
    return logger


def reset() -> None:
    """
    Drop every cached logger and close its files.

    For tests, which point the log directory somewhere throwaway and would
    otherwise keep writing into the previous test's folder.
    """
    for logger in _loggers.values():
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    _loggers.clear()


def log_path(name: str) -> str:
    """Where this task's log file is (or will be)."""
    return os.path.join(config.log_dir(), f"{name}.log")
