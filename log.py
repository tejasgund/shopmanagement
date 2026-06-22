"""
log.py - Centralized Logging Configuration
Tenant Management System

Features:
    - Daily rotating log files
    - Separate app and error logs
    - Gzip compression for old log files
    - Request/Response logging middleware
    - Exception logging with stack traces
"""

import os
import gzip
import shutil
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
LOG_DIR = "logs"
APP_LOG_FILE = os.path.join(LOG_DIR, "app.log")
ERROR_LOG_FILE = os.path.join(LOG_DIR, "error.log")
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# ──────────────────────────────────────────────
# Create logs directory if it doesn't exist
# ──────────────────────────────────────────────
def ensure_log_directory():
    """Create the /logs directory if it does not exist."""
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)


# ──────────────────────────────────────────────
# Gzip Rotator
# ──────────────────────────────────────────────
def gzip_rotator(source: str, dest: str):
    """
    Compress rotated log files into .gz format.
    Called automatically by TimedRotatingFileHandler after rotation.
    """
    with open(source, "rb") as f_in:
        with gzip.open(dest + ".gz", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    os.remove(source)


def gzip_namer(name: str) -> str:
    """Return the name for the rotated (compressed) log file."""
    return name  # .gz will be appended by gzip_rotator


# ──────────────────────────────────────────────
# Build a rotating file handler
# ──────────────────────────────────────────────
def _build_rotating_handler(filepath: str, level: int) -> TimedRotatingFileHandler:
    """
    Create a daily-rotating file handler with gzip compression.

    Args:
        filepath: Path to the log file.
        level: Logging level (e.g., logging.INFO).

    Returns:
        Configured TimedRotatingFileHandler.
    """
    handler = TimedRotatingFileHandler(
        filepath,
        when="midnight",   # Rotate at midnight
        interval=1,        # Every 1 day
        backupCount=30,    # Keep 30 days of logs
        encoding="utf-8",
        utc=False,
    )
    handler.rotator = gzip_rotator
    handler.namer = gzip_namer
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


# ──────────────────────────────────────────────
# Console handler
# ──────────────────────────────────────────────
def _build_console_handler() -> logging.StreamHandler:
    """Create a console (stdout) handler for development visibility."""
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


# ──────────────────────────────────────────────
# Logger factory
# ──────────────────────────────────────────────
def get_logger(name: str = "app") -> logging.Logger:
    """
    Return a named logger with app + error file handlers attached.
    Safe to call multiple times – handlers are not duplicated.

    Args:
        name: Logger name (defaults to 'app').

    Returns:
        Configured logging.Logger instance.
    """
    ensure_log_directory()

    logger = logging.getLogger(name)
    if logger.handlers:
        # Already configured – return as-is
        return logger

    logger.setLevel(logging.DEBUG)

    # logs/app.log  – INFO and above
    logger.addHandler(_build_rotating_handler(APP_LOG_FILE, logging.INFO))

    # logs/error.log – ERROR and above only
    logger.addHandler(_build_rotating_handler(ERROR_LOG_FILE, logging.ERROR))

    # Console – all levels during development
    logger.addHandler(_build_console_handler())

    # Prevent propagation to the root logger to avoid duplicate output
    logger.propagate = False

    return logger


# ──────────────────────────────────────────────
# FastAPI Request / Response Logging Middleware
# ──────────────────────────────────────────────
async def log_request_middleware(request, call_next):
    """
    ASGI middleware that logs every HTTP request and its response.

    Log format:
        2026-06-22 10:00:00 | INFO | POST /api/login | Status 200 | 25ms

    Usage in app.py:
        from log import log_request_middleware
        app.middleware("http")(log_request_middleware)
    """
    logger = get_logger("app")
    start_time = time.time()

    # Read and cache request body so it can be consumed more than once
    body_bytes = await request.body()

    # Log the incoming request
    try:
        body_preview = body_bytes.decode("utf-8")[:500]  # limit to 500 chars
    except Exception:
        body_preview = "<binary>"

    logger.info(
        "REQUEST  | %s %s | Body: %s",
        request.method,
        request.url.path,
        body_preview,
    )

    # Process the request
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.exception(
            "EXCEPTION | %s %s | %dms | %s",
            request.method,
            request.url.path,
            elapsed_ms,
            str(exc),
        )
        raise

    elapsed_ms = int((time.time() - start_time) * 1000)

    # Log the outgoing response
    logger.info(
        "%s %s | Status %s | %dms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )

    return response


# ──────────────────────────────────────────────
# Module-level default logger
# ──────────────────────────────────────────────
logger = get_logger("app")
