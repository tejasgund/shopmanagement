"""
scheduler/db_config.py - database setup for the cron process.

Separate from the application's core/database.py on purpose. This process is not
the API: it is short-lived, single-threaded, runs a handful of statements and
exits, so it wants its own small engine rather than the API's connection
pool. Keeping them apart also means the cron job can be pointed at a replica,
a different credential, or a different host without touching the app.

What it does NOT duplicate is the schema. The ORM models are imported from
the application's models/schema.py, so there is exactly one definition of
what a bill is - the same reason services/rent_billing.py is shared rather than
copied into this folder.
"""

import configparser
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler.conf")

# The application's own fallbacks, repeated here so that a cron process with
# no configuration at all still reaches the same database the app would.
_APP_DEFAULTS = {
    "host": "localhost",
    "port": "3306",
    "name": "tenant_management",
    "user": "root",
    "password": "root",
}


def _conf() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(CONF_PATH)
    return parser


def _setting(parser: configparser.ConfigParser, key: str, env_var: str) -> str:
    """
    One database setting, resolved conf-file first then environment.

    Blank is treated as "not set" rather than as an empty value, so an
    operator can leave the keys in scheduler.conf present-but-empty as
    documentation without accidentally configuring an empty password.
    """
    value = ""
    if parser.has_section("database"):
        value = (parser.get("database", key, fallback="") or "").strip()
    if value:
        return value
    return os.getenv(env_var, "") or _APP_DEFAULTS.get(key, "")


def database_url() -> str:
    """The URL this process should connect to. See scheduler.conf for precedence."""
    env_url = (os.getenv("DATABASE_URL") or "").strip()
    if env_url:
        return env_url

    parser = _conf()
    conf_url = ""
    if parser.has_section("database"):
        conf_url = (parser.get("database", "url", fallback="") or "").strip()
    if conf_url:
        return conf_url

    return (
        f"mysql+pymysql://{_setting(parser, 'user', 'DB_USER')}:"
        f"{_setting(parser, 'password', 'DB_PASSWORD')}@"
        f"{_setting(parser, 'host', 'DB_HOST')}:"
        f"{_setting(parser, 'port', 'DB_PORT')}/"
        f"{_setting(parser, 'name', 'DB_NAME')}?charset=utf8mb4"
    )


def make_session_factory():
    """
    A fresh engine + session factory for this run.

    pool_pre_ping matters more here than in the API: a cron job that fires
    once a day will routinely find the server has closed an idle connection,
    and without it the first statement of the night fails.
    """
    url = database_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def safe_url_for_logging() -> str:
    """The connection target with the password removed, for the run log."""
    url = database_url()
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
