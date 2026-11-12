"""
scheduler/config.py - everything this process needs to know before it can
reach the database or write a log line.

The scheduler reads its own scheduler.conf and its own environment. It never
imports the application's core/config.py, so the two can be deployed on
different machines, under different users, with different timezones and log
locations, and neither has to know the other exists.

Precedence for every value, highest first:

    1. the environment                 (cron entries can export these)
    2. scheduler.conf                  (usually the most reliable for cron)
    3. the fallbacks written here      (a scheduler with no config still runs)

Values are read fresh on every process start. Since every run IS a fresh
process, "changes take effect on the next run" needs no reload mechanism.
"""

import configparser
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.getenv("SCHEDULER_CONF") or os.path.join(HERE, "scheduler.conf")

# Fallbacks used when neither the environment nor scheduler.conf says
# otherwise. They match the application's own defaults so that a scheduler
# dropped onto a box with no configuration still finds the same database a
# default install of the app would have created - a convenience, not a
# dependency: nothing here is imported from the app.
_DB_FALLBACK = {
    "host": "localhost",
    "port": "3306",
    "name": "tenant_management",
    "user": "root",
    "password": "root",
}

DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_LOG_DIR = os.path.join(HERE, "logs")
DEFAULT_LOG_RETENTION_DAYS = 30
DEFAULT_LOG_LEVEL = "INFO"


def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(CONF_PATH)
    return parser


_CONF = _parser()


def _conf_value(section: str, key: str) -> str:
    """
    One value from scheduler.conf, or "" when absent.

    Blank is treated as "not set" rather than as an empty value, so an
    operator can leave a key present-but-empty as documentation without
    accidentally configuring an empty password or an empty log directory.
    """
    if not _CONF.has_section(section):
        return ""
    return (_CONF.get(section, key, fallback="") or "").strip()


def _resolve(section: str, key: str, env_var: str, fallback: str = "") -> str:
    """Environment, then conf file, then the built-in fallback."""
    env_value = (os.getenv(env_var) or "").strip()
    if env_value:
        return env_value
    conf_value = _conf_value(section, key)
    if conf_value:
        return conf_value
    return fallback


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def database_url() -> str:
    """
    The URL this process should connect to.

    A whole URL wins over the assembled parts, because a deployment that has
    one (a managed database, an SSL DSN, a replica) means it for everything.
    """
    whole = _resolve("database", "url", "DATABASE_URL")
    if whole:
        return whole

    return (
        f"mysql+pymysql://{_resolve('database', 'user', 'DB_USER', _DB_FALLBACK['user'])}:"
        f"{_resolve('database', 'password', 'DB_PASSWORD', _DB_FALLBACK['password'])}@"
        f"{_resolve('database', 'host', 'DB_HOST', _DB_FALLBACK['host'])}:"
        f"{_resolve('database', 'port', 'DB_PORT', _DB_FALLBACK['port'])}/"
        f"{_resolve('database', 'name', 'DB_NAME', _DB_FALLBACK['name'])}?charset=utf8mb4"
    )


def safe_database_url() -> str:
    """The connection target with the password removed, for the run log."""
    url = database_url()
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


# ══════════════════════════════════════════════════════════════════════════════
# TIME
# ══════════════════════════════════════════════════════════════════════════════

def timezone() -> str:
    """
    The timezone every scheduled time is interpreted in.

    This must match the timezone the application stores its timestamps in, or
    a task scheduled for 02:00 will look like it ran at the wrong hour on the
    dashboard. It is configured rather than imported so the two stay
    independent - see the README on keeping them in step.
    """
    return _resolve("scheduler", "timezone", "SCHEDULER_TIMEZONE", DEFAULT_TIMEZONE)


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def log_dir() -> str:
    """
    Where per-task log files are written. Relative paths are resolved against
    this folder, not the working directory, because cron's working directory
    is whatever it happens to be and a relative path would scatter log files
    across the filesystem.
    """
    configured = _resolve("logging", "dir", "SCHEDULER_LOG_DIR", DEFAULT_LOG_DIR)
    return configured if os.path.isabs(configured) else os.path.join(HERE, configured)


def log_retention_days() -> int:
    try:
        return max(1, int(_resolve("logging", "retention_days",
                                   "SCHEDULER_LOG_RETENTION_DAYS",
                                   str(DEFAULT_LOG_RETENTION_DAYS))))
    except ValueError:
        return DEFAULT_LOG_RETENTION_DAYS


def log_level() -> str:
    return _resolve("logging", "level", "SCHEDULER_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()


def log_to_console() -> bool:
    """
    Whether to also print to stdout.

    On by default: cron mails whatever a job prints, which is how an operator
    finds out something broke without going to look. Turn it off when the
    crontab already redirects output to a file.
    """
    raw = _resolve("logging", "console", "SCHEDULER_LOG_CONSOLE", "true")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")
