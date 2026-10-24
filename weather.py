"""Fetch hourly irradiance from Open-Meteo and store it in weather_samples.

Open-Meteo is free and needs no API key. shortwave_radiation is the mean
global horizontal irradiance (W/m²) over the preceding hour, so each stored
row is also that hour's insolation in Wh/m². The aggregator sums these per
local day to compute a performance ratio.

Run hourly from a systemd timer. Needs latitude/longitude in system_config
(python configure.py latitude 36.1 ; python configure.py longitude -95.9).
"""

import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import settings
from timeutil import UTC, now_utc, to_iso

API = "https://api.open-meteo.com/v1/forecast"
HOURLY = ("shortwave_radiation", "cloud_cover", "temperature_2m")


def build_url(lat, lon, past_days):
    query = urllib.parse.urlencode(
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "hourly": ",".join(HOURLY),
            "past_days": past_days,
            "forecast_days": 1,
            "timezone": "UTC",
        }
    )
    return f"{API}?{query}"


def fetch(lat, lon, past_days=settings.WEATHER_PAST_DAYS):
    req = urllib.request.Request(
        build_url(lat, lon, past_days), headers={"User-Agent": "SolarMonitor"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def rows_from_payload(payload, now=None):
    """Turn the hourly arrays into DB rows for completed hours only.

    Open-Meteo stamps each hour with its *start*. Row timestamp = hour end,
    so the value describes the hour ending at that timestamp.
    """
    now = now or now_utc()
    hourly = payload["hourly"]
    rows = []
    for i, start_text in enumerate(hourly["time"]):
        start = datetime.fromisoformat(start_text).replace(tzinfo=UTC)
        end = start + timedelta(hours=1)
        if end > now:
            continue
        ghi = hourly["shortwave_radiation"][i]
        if ghi is None:
            continue
        rows.append(
            (
                to_iso(end),
                hourly["temperature_2m"][i],
                hourly["cloud_cover"][i],
                ghi,
                "open-meteo",
            )
        )
    return rows


def store(conn, rows):
    with conn:
        conn.executemany(
            """
            INSERT INTO weather_samples (timestamp, temperature_c, cloud_cover_percent, ghi_wm2, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(timestamp) DO UPDATE SET
                temperature_c = excluded.temperature_c,
                cloud_cover_percent = excluded.cloud_cover_percent,
                ghi_wm2 = excluded.ghi_wm2,
                source = excluded.source
            """,
            rows,
        )
    return len(rows)


def site_coordinates(conn):
    cfg = dict(conn.execute("SELECT key, value FROM system_config").fetchall())
    lat, lon = cfg.get("latitude", ""), cfg.get("longitude", "")
    if lat == "" or lon == "":
        return None
    return float(lat), float(lon)


def main():
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        coords = site_coordinates(conn)
        if coords is None:
            print("Weather disabled: set latitude and longitude with configure.py")
            return 0
        try:
            payload = fetch(*coords)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"Weather fetch failed: {exc}")
            return 1
        count = store(conn, rows_from_payload(payload))
        print(f"Stored {count} hourly weather rows for {coords[0]:.3f}, {coords[1]:.3f}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
