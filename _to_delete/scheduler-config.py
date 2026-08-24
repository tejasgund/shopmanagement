"""
scheduler/config.py - reads scheduler.conf.

Only two questions are asked of the file: is the master switch on, and is
this particular job on. WHEN a job runs is crontab's business, so unlike the
old in-app scheduler_config.py this holds no cron fields - having the
schedule in two places was a way to change it in the file, restart nothing,
and wonder why the timing never moved.

A missing or malformed file must never silently disable billing, so every
value falls back to a working default and unparseable input is reported
rather than swallowed.
"""

import configparser
import os

CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler.conf")

_DEFAULTS = {
    "scheduler_enabled": True,
    "rent_bill_generation": {
        "enabled": True,
        "timezone": "Asia/Kolkata",
    },
}


def _get_bool(parser, section, key, default):
    try:
        return parser.getboolean(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default
    except ValueError:
        # A typo like `enabled = ture` must not read as False and quietly stop
        # billing - fall back to the default and say so on the run log.
        print(f"WARNING | scheduler.conf: [{section}] {key} is not a boolean; using {default}")
        return default


def _get_str(parser, section, key, default):
    try:
        value = (parser.get(section, key) or "").strip()
        return value or default
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def load() -> dict:
    """The resolved configuration, with defaults filled in."""
    parser = configparser.ConfigParser()
    if not os.path.exists(CONF_PATH):
        print(f"WARNING | scheduler.conf not found at {CONF_PATH}; using defaults")
        return {
            "scheduler_enabled": _DEFAULTS["scheduler_enabled"],
            "rent_bill_generation": dict(_DEFAULTS["rent_bill_generation"]),
        }

    parser.read(CONF_PATH)
    return {
        "scheduler_enabled": _get_bool(
            parser, "scheduler", "enabled", _DEFAULTS["scheduler_enabled"]
        ),
        "rent_bill_generation": {
            "enabled": _get_bool(
                parser, "rent_bill_generation", "enabled",
                _DEFAULTS["rent_bill_generation"]["enabled"],
            ),
            "timezone": _get_str(
                parser, "rent_bill_generation", "timezone",
                _DEFAULTS["rent_bill_generation"]["timezone"],
            ),
        },
    }
