"""
scheduler_config.py - Loads conf/scheduler.conf

Lets the background job scheduler's on/off state and cron timing be
changed by editing a config file instead of app.py. Falls back to safe
defaults (job enabled, daily 02:00 Asia/Kolkata) if the file is missing
or a value can't be parsed, so a bad/absent conf never prevents startup.
"""

import configparser
import os

CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conf", "scheduler.conf")

_DEFAULTS = {
    "scheduler_enabled": True,
    "rent_bill_generation": {
        "enabled": True,
        "minute": "0",
        "hour": "2",
        "day": "*",
        "month": "*",
        "day_of_week": "*",
        "timezone": "Asia/Kolkata",
        "misfire_grace_time": 3600,
    },
}


def _get_bool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    try:
        return parser.getboolean(section, key, fallback=default)
    except ValueError:
        return default


def load_scheduler_config(path: str = CONF_PATH) -> dict:
    """Returns a dict shaped like _DEFAULTS, filled in from the conf file
    where present and valid, otherwise defaulted."""
    config = {
        "scheduler_enabled": _DEFAULTS["scheduler_enabled"],
        "rent_bill_generation": dict(_DEFAULTS["rent_bill_generation"]),
    }

    parser = configparser.ConfigParser()
    if not parser.read(path):
        return config

    config["scheduler_enabled"] = _get_bool(parser, "scheduler", "enabled", config["scheduler_enabled"])

    job = config["rent_bill_generation"]
    section = "rent_bill_generation"
    job["enabled"] = _get_bool(parser, section, "enabled", job["enabled"])
    for cron_field in ("minute", "hour", "day", "month", "day_of_week"):
        job[cron_field] = parser.get(section, cron_field, fallback=job[cron_field]).strip()
    job["timezone"] = parser.get(section, "timezone", fallback=job["timezone"]).strip()

    try:
        job["misfire_grace_time"] = parser.getint(section, "misfire_grace_time", fallback=job["misfire_grace_time"])
    except ValueError:
        pass

    return config
