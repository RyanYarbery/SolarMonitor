"""Runtime settings for all SolarMonitor processes.

Every value can be overridden with an environment variable so the same code
runs unchanged on the Pi (via EnvironmentFile= in the systemd units) and on a
development machine.
"""

import os
from pathlib import Path


def _env(name, default, cast=str):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return cast(value)


def _bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Storage
DB_PATH = Path(_env("SOLAR_DB_PATH", "solar_v1.db"))
LIVE_PATH = Path(_env("SOLAR_LIVE_PATH", "/run/solar-monitor/latest.json"))

# Serial link to the directly connected inverter
SERIAL_PORT = _env("SOLAR_SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = _env("SOLAR_SERIAL_BAUD", 2400, int)
SERIAL_TIMEOUT = _env("SOLAR_SERIAL_TIMEOUT", 3.0, float)
# Pause between consecutive commands; PI30 inverters NAK if hammered.
SERIAL_COMMAND_GAP = _env("SOLAR_SERIAL_COMMAND_GAP", 0.2, float)
# Reject frames whose CRC does not verify. Set to 0 only if your firmware is
# known to emit bad CRCs; mismatches are logged either way.
SERIAL_STRICT_CRC = _env("SOLAR_SERIAL_STRICT_CRC", True, _bool)

# Collector
SAMPLE_INTERVAL = _env("SOLAR_SAMPLE_INTERVAL", 10, int)
# A live reading older than this is reported as stale by the dashboard.
STALE_AFTER_SECONDS = _env("SOLAR_STALE_AFTER", 60, int)

# Retention (days). 0 means keep forever.
RAW_RETENTION_DAYS = _env("SOLAR_RAW_RETENTION_DAYS", 7, int)
MINUTE_RETENTION_DAYS = _env("SOLAR_MINUTE_RETENTION_DAYS", 365, int)

# Dashboard
DASHBOARD_HOST = _env("SOLAR_DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = _env("SOLAR_DASHBOARD_PORT", 5000, int)
