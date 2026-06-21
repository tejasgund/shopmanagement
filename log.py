"""
================================================================================
 log.py
================================================================================
 Centralized logging configuration for the Shop Electricity Bill Management
 System. Import `get_logger` anywhere in the project to obtain a consistently
 formatted logger.

 Usage:
     from log import get_logger
     logger = get_logger("main")
     logger.info("Server starting...")
================================================================================
"""
import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Keep track of loggers we've already configured so we never attach
# duplicate handlers if get_logger() is called multiple times for the
# same name.
_configured_loggers = {}


def get_logger(name: str = "app", level: int = logging.INFO) -> logging.Logger:
    """
    Returns a configured logger instance that writes to stdout.

    Args:
        name: Logger name, typically the module name (e.g. "main", "createMN").
        level: Logging level (default INFO).

    Returns:
        A ready-to-use logging.Logger.
    """
    if name in _configured_loggers:
        return _configured_loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        # Avoid duplicate log lines bubbling up to the root logger
        logger.propagate = False

    _configured_loggers[name] = logger
    return logger
