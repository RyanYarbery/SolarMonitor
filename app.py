"""Flask dashboard and JSON API."""

import csv
import io
import json
import sqlite3
from datetime import timedelta

from flask import Flask, Response, jsonify, render_template_string, request

import settings
from aggregator import integrate_energy
from collector import NO_FAULT
from eg4 import MODES
from timeutil import local_day_bounds, now_utc, parse_iso, to_iso, today_local

app = Flask(__name__)

MAX_HISTORY_HOURS = 24 * 365 * 3
MAX_BARS = 130


def connect_db():
    conn = sqlite3.connect(settings.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


# ----------------------------------------------------------------------- live


def get_latest_reading():
    reading = None
    try:
        with settings.LIVE_PATH.open("r") as f:
            reading = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if reading is None:
        conn = connect_db()
        try:
            row = conn.execute(
                """
                SELECT timestamp, total_pv_watts AS pv_watts,
                       total_load_watts AS load_watts, battery_voltage,
                       battery_soc, battery_watts, grid_watts,
                       inverter_count, source
                FROM system_samples ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            inverters = _latest_inverter_rows(conn) if row is not None else []
        finally:
            conn.close()
        if row is None:
            return None
        reading = dict(row)
        # The live snapshot carries these; derive them so the AC-input tile and
        # fault banner still work when the snapshot is missing (first start,
        # or the dashboard running on another host against a copied database).
        volts = [i["grid_voltage"] for i in inverters if i["grid_voltage"] is not None]
        reading["grid_voltage"] = max(volts) if volts else None
        reading["grid_present"] = (
            None if not volts else reading["grid_voltage"] >= settings.GRID_ON_VOLTS
        )
        reading["any_fault"] = any((i["fault_code"] or "") not in NO_FAULT for i in inverters)

    try:
        age = (now_utc() - parse_iso(reading["timestamp"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = None
    reading["age_seconds"] = None if age is None else int(age)
    reading["stale"] = age is None or age > settings.STALE_AFTER_SECONDS
    return reading


def _latest_inverter_rows(conn):
    return conn.execute(
        """
        SELECT s.timestamp, s.inverter_id, s.grid_voltage, s.output_voltage,
               s.output_frequency, s.output_watts, s.output_va, s.load_percent,
               s.battery_voltage, s.battery_soc, s.pv_voltage, s.pv_current, s.pv_watts,
               s.pv1_voltage, s.pv1_current, s.pv1_watts,
               s.pv2_voltage, s.pv2_current, s.pv2_watts,
               s.inverter_temperature_c, s.mode, s.fault_code, s.status
        FROM inverter_samples AS s
        INNER JOIN (
            SELECT inverter_id, MAX(id) AS max_id
            FROM inverter_samples GROUP BY inverter_id
        ) AS latest ON s.id = latest.max_id
        ORDER BY s.inverter_id
        """
    ).fetchall()


def get_latest_inverters():
    conn = connect_db()
    try:
        rows = _latest_inverter_rows(conn)
    finally:
        conn.close()

    result = []
    for row in rows:
        item = dict(row)
        item["mode_name"] = MODES.get(item.get("mode"), "unknown")
        status = item.get("status") or ""
        item["pv_quality"] = (
            "measured"
            if "pv_measured" in status
            else "estimated"
            if "pv_estimated" in status
            else "unknown"
        )
        result.append(item)
    return result


def get_recent_events(limit=50):
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            SELECT id, timestamp, event_type, severity, source, inverter_id, message, cleared_at
            FROM events ORDER BY timestamp DESC, id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------- summary


def energy_today(conn):
    """Today's PV and load energy: completed quarter-hours plus the raw tail.

    Integrating a whole day of 10-second samples on every refresh is too much
    for a Pi 1, so the roll-ups do the heavy lifting.
    """
    day_start, day_end = local_day_bounds(today_local())
    row = conn.execute(
        "SELECT SUM(pv_energy_wh), SUM(load_energy_wh), MAX(timestamp) "
        "FROM quarter_hour_summary WHERE timestamp >= ? AND timestamp < ?",
        (to_iso(day_start), to_iso(day_end)),
    ).fetchone()
    pv_wh = row[0] or 0.0
    load_wh = row[1] or 0.0
    # Quarter buckets are keyed by start; the tail begins at the last bucket's end.
    tail_from = parse_iso(row[2]) + timedelta(minutes=15) if row[2] else day_start
    tail = conn.execute(
        "SELECT timestamp, total_pv_watts, total_load_watts FROM system_samples "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (to_iso(tail_from), to_iso(day_end)),
    ).fetchall()
    pv_wh += integrate_energy(tail, "total_pv_watts")
    load_wh += integrate_energy(tail, "total_load_watts")
    return {"pv_wh": round(pv_wh), "load_wh": round(load_wh)}


def battery_runtime(reading, capacity_kwh):
    """Hours to empty (discharging) or to full (charging) at the current rate."""
    if not reading or not capacity_kwh or capacity_kwh <= 0:
        return None
    soc = reading.get("battery_soc")
    watts = reading.get("battery_watts")
    if soc is None or watts is None or abs(watts) < 25:
        return {"state": "idle", "hours": None}
    capacity_wh = capacity_kwh * 1000.0
    if watts < 0:
        return {"state": "discharging", "hours": round(soc / 100.0 * capacity_wh / -watts, 1)}
    return {"state": "charging", "hours": round((100.0 - soc) / 100.0 * capacity_wh / watts, 1)}


def get_summary():
    conn = connect_db()
    try:
        config = dict(conn.execute("SELECT key, value FROM system_config").fetchall())
        yesterday = conn.execute(
            "SELECT pv_energy_wh, load_energy_wh, battery_soc_min, pv_peak_watts, "
            "pv_peak_time, insolation_wh_m2, performance_ratio "
            "FROM daily_summary WHERE date = ?",
            ((today_local() - timedelta(days=1)).isoformat(),),
        ).fetchone()
        today = energy_today(conn)
        day_start = to_iso(local_day_bounds(today_local())[0])
        soc_min_today, pv_peak_today = conn.execute(
            "SELECT MIN(battery_soc), MAX(total_pv_watts) FROM system_samples WHERE timestamp >= ?",
            (day_start,),
        ).fetchone()
        pv_peak_time = None
        if pv_peak_today:
            pv_peak_time = conn.execute(
                "SELECT timestamp FROM system_samples WHERE timestamp >= ? "
                "AND total_pv_watts = ? ORDER BY timestamp LIMIT 1",
                (day_start, pv_peak_today),
            ).fetchone()[0]
    finally:
        conn.close()

    reading = get_latest_reading()
    capacity = float(config.get("battery_capacity_kwh") or 0)
    return {
        "today": dict(
            today,
            battery_soc_min=soc_min_today,
            pv_peak_watts=pv_peak_today,
            pv_peak_time=pv_peak_time,
        ),
        "has_coordinates": bool(config.get("latitude") and config.get("longitude")),
        "yesterday": dict(yesterday) if yesterday else None,
        "battery": battery_runtime(reading, capacity),
        "battery_capacity_kwh": capacity or None,
        "installed_pv_kw": float(config.get("installed_pv_kw") or 0) or None,
    }


DAY_COLUMNS = [
    "date",
    "pv_energy_wh",
    "load_energy_wh",
    "battery_charge_wh",
    "battery_discharge_wh",
    "pv_peak_watts",
    "pv_peak_time",
    "load_peak_watts",
    "production_minutes",
    "battery_soc_min",
    "battery_soc_max",
    "battery_round_trip_efficiency",
    "equivalent_battery_cycles",
    "specific_yield_kwh_per_kw",
    "insolation_wh_m2",
    "performance_ratio",
    "inverter_1_pv_wh",
    "inverter_2_pv_wh",
]


def get_days(days=90):
    conn = connect_db()
    try:
        rows = conn.execute(
            "SELECT * FROM daily_summary WHERE date >= ? ORDER BY date DESC",
            ((today_local() - timedelta(days=days)).isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [{k: r[k] for k in DAY_COLUMNS} for r in rows]


def days_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=DAY_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# -------------------------------------------------------------------- history


def add_gap_markers(rows, expected_seconds):
    points = []
    previous = None
    for row in rows:
        point = dict(row)
        try:
            current = parse_iso(point["timestamp"])
        except (TypeError, ValueError):
            current = None
        point["gap_before"] = (
            previous is not None
            and current is not None
            and (current - previous).total_seconds() > expected_seconds * 1.8
        )
        if current is not None:
            previous = current
        points.append(point)
    return points


def _bucket_group(bucket_seconds):
    # Stored timestamps carry "+00:00"; SQLite's strftime honours the offset.
    return f"GROUP BY CAST(strftime('%s', timestamp) AS INTEGER) / {int(bucket_seconds)}"


def get_recent_readings(hours=24, max_points=720):
    hours = max(1, int(hours))
    max_points = max(10, int(max_points))
    now = now_utc()
    conn = connect_db()
    try:
        if hours <= 1:
            bucket_seconds = max(1, int(hours * 3600 / max_points))
            rows = conn.execute(
                f"""
                SELECT MIN(timestamp) AS timestamp,
                       AVG(total_pv_watts) AS pv_value,
                       AVG(total_load_watts) AS load_value,
                       AVG(battery_soc) AS battery_soc
                FROM system_samples
                WHERE timestamp >= ?
                {_bucket_group(bucket_seconds)}
                ORDER BY MIN(timestamp)
                """,
                (to_iso(now - timedelta(hours=hours)),),
            ).fetchall()
            return _history("raw", rows, max(60, bucket_seconds * 3))

        if hours <= 24:
            bucket_minutes = max(1, -(-hours * 60 // max_points))
            bucket_seconds = bucket_minutes * 60
            rows = conn.execute(
                f"""
                SELECT MIN(timestamp) AS timestamp,
                       AVG(pv_watts_avg) AS pv_value,
                       AVG(load_watts_avg) AS load_value,
                       AVG(battery_soc_avg) AS battery_soc
                FROM minute_summary
                WHERE timestamp >= ?
                {_bucket_group(bucket_seconds)}
                ORDER BY MIN(timestamp)
                """,
                (to_iso(now - timedelta(hours=hours)),),
            ).fetchall()
            return _history("minute", rows, max(180, bucket_seconds * 3))

        if hours <= 24 * 31:
            bucket_quarters = max(1, -(-hours * 4 // max_points))
            bucket_seconds = bucket_quarters * 900
            rows = conn.execute(
                f"""
                SELECT MIN(timestamp) AS timestamp,
                       AVG(pv_energy_wh * 4.0) AS pv_value,
                       AVG(load_energy_wh * 4.0) AS load_value,
                       AVG((battery_soc_min + battery_soc_max) / 2.0) AS battery_soc,
                       MIN(battery_soc_min) AS battery_soc_min,
                       MAX(battery_soc_max) AS battery_soc_max
                FROM quarter_hour_summary
                WHERE timestamp >= ?
                {_bucket_group(bucket_seconds)}
                ORDER BY MIN(timestamp)
                """,
                (to_iso(now - timedelta(hours=hours)),),
            ).fetchall()
            return _history("quarter_hour", rows, max(2700, bucket_seconds * 3))

        # Energy per day draws as bars, which stop being readable beyond
        # about 130 of them; fold longer ranges into calendar weeks or months.
        days = max(1, int(round(hours / 24)))
        if days <= MAX_BARS:
            bucket, group, gap_days = "day", "date", 1
        elif days <= MAX_BARS * 7:
            # julianday(...) + 1 makes integer division land on Mondays.
            bucket, group, gap_days = "week", "(CAST(julianday(date) AS INTEGER) + 1) / 7", 7
        else:
            bucket, group, gap_days = "month", "strftime('%Y-%m', date)", 31
        rows = conn.execute(
            f"""
            SELECT MIN(date) || 'T12:00:00' AS timestamp,
                   AVG(pv_energy_wh) / 1000.0 AS pv_value,
                   AVG(load_energy_wh) / 1000.0 AS load_value,
                   AVG((battery_soc_min + battery_soc_max) / 2.0) AS battery_soc,
                   MIN(battery_soc_min) AS battery_soc_min,
                   MAX(battery_soc_max) AS battery_soc_max,
                   COUNT(*) AS days
            FROM daily_summary
            WHERE date >= ?
            GROUP BY {group}
            ORDER BY MIN(date)
            """,
            ((today_local() - timedelta(days=days)).isoformat(),),
        ).fetchall()
        return {
            "mode": "daily_energy",
            "source": "daily",
            "bucket": bucket,
            "pv_unit": "kWh/day",
            "load_unit": "kWh/day",
            "points": add_gap_markers(rows, gap_days * 86400),
        }
    finally:
        conn.close()


def _history(source, rows, expected_gap):
    return {
        "mode": "power",
        "source": source,
        "pv_unit": "W",
        "load_unit": "W",
        "points": add_gap_markers(rows, expected_gap),
    }


# --------------------------------------------------------------------- routes


@app.route("/")
def dashboard():
    return render_template_string(HTML)


@app.route("/api/latest")
def api_latest():
    reading = get_latest_reading()
    if reading is None:
        return jsonify({"error": "No readings available"}), 404
    return jsonify(reading)


@app.route("/api/inverters")
def api_inverters():
    return jsonify(get_latest_inverters())


@app.route("/api/events")
def api_events():
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except (TypeError, ValueError):
        limit = 50
    return jsonify(get_recent_events(limit))


@app.route("/api/summary")
def api_summary():
    return jsonify(get_summary())


def _days_arg():
    try:
        return max(1, min(int(request.args.get("days", 90)), 3660))
    except (TypeError, ValueError):
        return 90


@app.route("/api/days")
def api_days():
    return jsonify(get_days(_days_arg()))


@app.route("/api/days.csv")
def api_days_csv():
    days = _days_arg()
    return Response(
        days_csv(get_days(days)),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=solar-days-{days}.csv"},
    )


@app.route("/days")
def days_page():
    return render_template_string(DAYS_HTML)


@app.route("/api/history")
def api_history():
    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, MAX_HISTORY_HOURS))
    return jsonify(get_recent_readings(hours=hours, max_points=720))


@app.route("/healthz")
def healthz():
    reading = get_latest_reading()
    ok = reading is not None and not reading["stale"]
    return jsonify({"ok": ok, "age_seconds": reading and reading["age_seconds"]}), (
        200 if ok else 503
    )


# ------------------------------------------------------------------ dashboard


# Shared look for both pages. No external assets: the Pi may be offline.
BASE_CSS = r"""
  :root {
    --bg: #111; --surface: #1b1b1b; --surface-2: #242424; --line: #2c2c2c; --line-2: #3a3a3a;
    --text: #ececec; --muted: #a6a6a6; --faint: #6f6f6f;
    --solar: #c98500; --load: #3987e5; --soc: #199e70;
    --good: #3fb950; --warn: #e3a008; --bad: #e66767; --info: #9ecbff;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body { margin: 0; padding: 0 16px 32px; background: var(--bg); color: var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         font-size: 15px; line-height: 1.35; }
  a { color: var(--info); }
  .topbar { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; padding: 14px 0 10px;
            border-bottom: 1px solid var(--line); margin-bottom: 16px; }
  .brand { font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }
  .topbar nav { display: flex; gap: 4px; }
  .topbar nav a { color: var(--muted); text-decoration: none; padding: 5px 10px; border-radius: 6px; font-size: 14px; }
  .topbar nav a:hover { color: var(--text); background: var(--surface); }
  .topbar nav a.active { color: var(--text); background: var(--surface-2); }
  .status { margin-left: auto; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--faint); flex: none; }
  .dot.live { background: var(--good); box-shadow: 0 0 0 3px rgba(63,185,80,0.18); }
  .dot.stale { background: var(--warn); }
  .dot.down { background: var(--bad); }
  h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em;
       color: var(--muted); margin: 26px 0 10px; }
  .card { background: var(--surface); border-radius: 10px; padding: 14px 16px; }
  .num { font-variant-numeric: tabular-nums; }
  .btn, button, select { border: 1px solid var(--line-2); background: var(--surface); color: var(--text);
         padding: 6px 12px; border-radius: 7px; cursor: pointer; font: inherit; font-size: 13px; text-decoration: none; }
  .btn:hover, button:hover { background: var(--surface-2); }
  .muted { color: var(--muted); }
"""


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Solar Monitor</title>
<style>
__BASE_CSS__
  .banner { display: none; margin-bottom: 16px; padding: 12px 16px; border-radius: 8px;
            background: #4a2600; color: #ffd9b3; border: 1px solid #8a4a00; }
  .banner.fault { background: #4a0f0f; color: #ffc9c9; border-color: #8a2a2a; }
  .banner.show { display: block; }

  .tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
  .tiles.secondary { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
  .tile { background: var(--surface); border-radius: 10px; padding: 14px 16px 12px; position: relative; overflow: hidden; }
  .tile::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--accent, transparent); }
  .tile .label { color: var(--muted); font-size: 13px; }
  .tile .value { margin-top: 6px; font-size: 30px; font-weight: 700; line-height: 1.1; letter-spacing: -0.3px; }
  .tiles.secondary .value { font-size: 24px; }
  .tile .value small { font-size: 15px; font-weight: 500; color: var(--muted); margin-left: 2px; }
  .tile .sub { margin-top: 6px; font-size: 13px; color: var(--muted); min-height: 1.2em; }
  .tile.solar { --accent: var(--solar); } .tile.load { --accent: var(--load); } .tile.battery { --accent: var(--soc); }
  body.stale .tile .value, body.stale .inv .v { opacity: 0.5; }
  .bar-track { height: 6px; border-radius: 3px; background: var(--surface-2); margin-top: 10px; overflow: hidden; }
  .bar-fill { height: 100%; width: 0; border-radius: 3px; background: var(--good); transition: width 0.6s ease; }
  .bar-fill.warn { background: var(--warn); } .bar-fill.bad { background: var(--bad); }

  .inverters { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
  .inv-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .inv-head h3 { margin: 0; font-size: 15px; font-weight: 600; }
  .chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: var(--surface-2); color: var(--muted); white-space: nowrap; }
  .chip.fault { background: #4a0f0f; color: #ffc9c9; }
  .chip.ok { color: var(--good); }
  .kv { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
  .kv .row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
             padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
  .kv .k { color: var(--muted); }
  .kv .v { font-weight: 600; text-align: right; }
  .kv .v small { font-weight: 400; color: var(--muted); font-size: 12px; margin-left: 4px; }
  .kv .v.fault { color: var(--bad); }
  @media (max-width: 420px) { .kv { grid-template-columns: 1fr; } }

  .history-head { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin: 26px 0 10px; }
  .history-head h2 { margin: 0; }
  .ranges { display: flex; gap: 2px; flex-wrap: wrap; background: var(--surface); padding: 3px; border-radius: 8px; }
  .ranges button { border: 0; background: transparent; color: var(--muted); padding: 5px 9px; border-radius: 6px; font-size: 13px; }
  .ranges button:hover { color: var(--text); }
  .ranges button.active { background: var(--surface-2); color: var(--text); font-weight: 600; }
  .chart-card { margin-top: 12px; padding: 14px 16px 10px; position: relative; }
  .chart-head { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 6px; }
  .chart-title { font-size: 14px; font-weight: 600; }
  .legend { display: flex; gap: 14px; font-size: 13px; color: var(--muted); }
  .legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 3px;
                         margin-right: 6px; vertical-align: -1px; background: var(--c); }
  .chart-card canvas { width: 100%; height: 240px; display: block; touch-action: pan-y; cursor: crosshair; }
  @media (max-width: 600px) { .chart-card canvas { height: 200px; } }
  .tip { position: absolute; pointer-events: none; display: none; background: #000; color: var(--text);
         border: 1px solid var(--line-2); border-radius: 7px; padding: 7px 10px; font-size: 12.5px;
         white-space: nowrap; z-index: 2; box-shadow: 0 4px 16px rgba(0,0,0,0.5); }
  .tip .t { color: var(--muted); margin-bottom: 4px; }
  .tip .r { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .tip .r i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
  .tip .r b { font-weight: 600; min-width: 3.5em; text-align: right; }

  table.events { width: 100%; border-collapse: collapse; font-size: 14px; }
  table.events td { padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  table.events tr:last-child td { border-bottom: 0; }
  table.events td.time { color: var(--muted); white-space: nowrap; width: 1%; }
  table.events td.type { white-space: nowrap; width: 1%; }
  .sev { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; vertical-align: 0; background: var(--faint); }
  .sev.info { background: var(--info); } .sev.warning { background: var(--warn); } .sev.error, .sev.critical { background: var(--bad); }
  .events-wrap { padding: 4px 8px; }
  @media (max-width: 600px) {
    body { padding: 0 12px 24px; }
    .tiles, .tiles.secondary { grid-template-columns: 1fr 1fr; gap: 10px; }
    .tile { padding: 12px 12px 10px; }
    .tile .value { font-size: 24px; } .tiles.secondary .value { font-size: 20px; }
    .tile .sub { font-size: 12px; }
    table.events td.type { display: none; }
  }
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">Solar Monitor</div>
  <nav><a href="/" class="active">Live</a><a href="/days">Daily table</a></nav>
  <div class="status"><span class="dot" id="statusDot"></span><span id="status">Waiting for telemetry…</span></div>
</header>
<div class="banner" id="banner"></div>

<div class="tiles">
  <div class="tile solar"><div class="label">Solar production</div>
    <div class="value num" id="pvWatts">--</div>
    <div class="sub num" id="pvSub">today -- · yesterday --</div></div>
  <div class="tile load"><div class="label">House load</div>
    <div class="value num" id="loadWatts">--</div>
    <div class="sub num" id="loadSub">today -- · yesterday --</div></div>
  <div class="tile battery"><div class="label">Battery</div>
    <div class="value num" id="batterySoc">--</div>
    <div class="bar-track"><div class="bar-fill" id="socBar"></div></div>
    <div class="sub num" id="batterySub">--</div></div>
  <div class="tile"><div class="label">Utility / AC input</div>
    <div class="value" id="grid">--</div>
    <div class="sub num" id="gridLabel"></div></div>
</div>

<div class="tiles secondary" style="margin-top:12px">
  <div class="tile"><div class="label">Peak solar today</div><div class="value num" id="pvPeak">--</div>
    <div class="sub num" id="pvPeakLabel">yesterday --</div></div>
  <div class="tile"><div class="label">Battery runtime</div><div class="value num" id="runtime">--</div>
    <div class="sub" id="runtimeLabel">set battery_capacity_kwh to enable</div></div>
  <div class="tile"><div class="label">Lowest SOC today</div><div class="value num" id="socMinToday">--</div>
    <div class="sub num" id="socMinYesterday">yesterday --</div></div>
  <div class="tile"><div class="label">Yesterday's performance</div><div class="value num" id="prYesterday">--</div>
    <div class="sub" id="prLabel"></div></div>
</div>

<h2>Inverters</h2>
<div class="inverters" id="inverters"><div class="card muted">Waiting for inverter telemetry…</div></div>

<div class="history-head">
  <h2 id="historyTitle">Last 24 hours</h2>
  <div class="ranges" id="ranges">
    <button data-hours="1">1h</button><button data-hours="6">6h</button>
    <button data-hours="24" class="active">24h</button><button data-hours="168">7d</button>
    <button data-hours="336">2w</button><button data-hours="720">1m</button>
    <button data-hours="2160">3m</button><button data-hours="4320">6m</button>
    <button data-hours="8760">1y</button><button data-hours="17520">2y</button>
    <button data-hours="26280">3y</button>
  </div>
</div>

<div class="card chart-card">
  <div class="chart-head"><div class="chart-title" id="powerTitle">Power</div>
    <div class="legend"><span style="--c:var(--solar)" id="pvLegend">Solar</span><span style="--c:var(--load)" id="loadLegend">Load</span></div></div>
  <canvas id="powerChart" role="img" aria-label="Solar production and house load over time"></canvas><div class="tip" id="powerTip"></div>
</div>
<div class="card chart-card">
  <div class="chart-head"><div class="chart-title">Battery state of charge</div>
    <div class="legend" id="socLegend"></div></div>
  <canvas id="socChart" role="img" aria-label="Battery state of charge over time"></canvas><div class="tip" id="socTip"></div>
</div>

<h2>Recent events</h2>
<div class="card events-wrap"><table class="events"><tbody id="events"><tr><td class="muted">No events recorded.</td></tr></tbody></table></div>

<script>
"use strict";
let historyHours = 24;
const LABELS = {1:"Last hour",6:"Last 6 hours",24:"Last 24 hours",168:"Last 7 days",336:"Last 2 weeks",
  720:"Last month",2160:"Last 3 months",4320:"Last 6 months",8760:"Last year",17520:"Last 2 years",26280:"Last 3 years"};
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isNum = v => v !== null && v !== undefined && Number.isFinite(Number(v));
const fmt = (v, d, unit) => isNum(v) ? Number(v).toFixed(d) + unit : "--";
const watts = v => !isNum(v) ? "--" : Math.abs(v) >= 10000 ? (v / 1000).toFixed(1) + " kW" : Math.round(v) + " W";
const kwh = wh => isNum(wh) ? (wh / 1000).toFixed(1) + " kWh" : "--";
const ageText = s => s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h";
const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
function whenText(iso) {
  const d = new Date(iso); if (!Number.isFinite(d.getTime())) return iso;
  const now = new Date(), yest = new Date(now); yest.setDate(now.getDate() - 1);
  const t = d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  if (sameDay(d, now)) return "Today " + t;
  if (sameDay(d, yest)) return "Yesterday " + t;
  return d.toLocaleDateString([], {month: "short", day: "numeric"}) + ", " + t;
}
const timeText = iso => { const d = new Date(iso); return Number.isFinite(d.getTime()) ? d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) : ""; };
const setDot = cls => { $("statusDot").className = "dot " + cls; };

let latest = {};
async function updateDashboard() {
  const banner = $("banner");
  try {
    const r = await fetch("/api/latest");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = latest = await r.json();
    $("pvWatts").textContent = watts(d.pv_watts);
    $("loadWatts").textContent = watts(d.load_watts);
    $("batterySoc").innerHTML = isNum(d.battery_soc) ? Math.round(d.battery_soc) + "<small>%</small>" : "--";
    const soc = Number(d.battery_soc);
    const fill = $("socBar");
    fill.style.width = isNum(d.battery_soc) ? Math.max(0, Math.min(100, soc)) + "%" : "0";
    fill.className = "bar-fill" + (soc < 25 ? " bad" : soc < 40 ? " warn" : "");
    const bw = Number(d.battery_watts);
    const flow = !isNum(d.battery_watts) ? "" : Math.abs(bw) < 25 ? "idle" : bw > 0 ? "charging at " + watts(bw) : "discharging at " + watts(-bw);
    $("batterySub").textContent = [fmt(d.battery_voltage, 1, " V"), flow].filter(Boolean).join(" · ");
    if (d.grid_present === true) {
      $("grid").textContent = "On";
      $("gridLabel").textContent = fmt(d.grid_voltage, 0, " V") + " at the input";
    } else if (d.grid_present === false) {
      $("grid").textContent = "Off";
      $("gridLabel").textContent = "running on solar and battery";
    } else { $("grid").textContent = "--"; $("gridLabel").textContent = ""; }
    const when = new Date(d.timestamp);
    $("status").textContent = "Updated " + (Number.isFinite(when.getTime()) ? when.toLocaleTimeString([], {hour: "numeric", minute: "2-digit", second: "2-digit"}) : d.timestamp)
      + " · " + d.inverter_count + " inverters · " + d.source;
    document.body.classList.toggle("stale", !!d.stale);
    document.title = (isNum(d.pv_watts) ? (d.pv_watts / 1000).toFixed(1) + " kW · " : "") + (isNum(d.battery_soc) ? Math.round(d.battery_soc) + "% · " : "") + "Solar Monitor";
    banner.className = "banner";
    if (d.stale) {
      setDot("stale");
      banner.textContent = "Telemetry is stale: last reading was " + ageText(d.age_seconds) + " ago. The collector may be down.";
      banner.classList.add("show");
    } else if (d.any_fault) {
      setDot("live");
      banner.textContent = "An inverter is reporting a fault. See the inverter cards and events below.";
      banner.classList.add("show", "fault");
    } else setDot("live");
  } catch (e) {
    setDot("down");
    $("status").textContent = "Telemetry unavailable: " + e.message;
  }
}

function inverterCard(inv) {
  const faulted = inv.fault_code && !["", "00", "0"].includes(inv.fault_code);
  const row = (k, v, small) => `<div class="row"><span class="k">${k}</span><span class="v num${v === "fault" ? " fault" : ""}">${v}${small ? `<small>${small}</small>` : ""}</span></div>`;
  return `<div class="card inv">
    <div class="inv-head"><h3>Inverter ${esc(inv.inverter_id)}</h3>
      <span class="chip" title="mode ${esc(inv.mode || "?")}">${esc((inv.mode_name || "--").replace(/_/g, " "))}</span>
      ${faulted ? `<span class="chip fault">Fault ${esc(inv.fault_code)}</span>` : `<span class="chip ok">no fault</span>`}
      <span class="chip">PV ${esc(inv.pv_quality)}</span></div>
    <div class="kv">
      ${row("Output", watts(inv.output_watts), fmt(inv.load_percent, 0, "% load"))}
      ${row("PV total", watts(inv.pv_watts), fmt(inv.pv_voltage, 0, " V"))}
      ${row("PV string 1", watts(inv.pv1_watts), fmt(inv.pv1_voltage, 0, " V") + " · " + fmt(inv.pv1_current, 1, " A"))}
      ${row("PV string 2", watts(inv.pv2_watts), fmt(inv.pv2_voltage, 0, " V") + " · " + fmt(inv.pv2_current, 1, " A"))}
      ${row("AC output", fmt(inv.output_voltage, 1, " V"), fmt(inv.output_frequency, 1, " Hz"))}
      ${row("Battery", fmt(inv.battery_voltage, 1, " V"), fmt(inv.battery_soc, 0, "%"))}
      ${row("Temperature", fmt(inv.inverter_temperature_c, 0, " °C"))}
      ${row("Last sample", esc(whenText(inv.timestamp)))}
    </div></div>`;
}

async function updateInverters() {
  try {
    const r = await fetch("/api/inverters");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const invs = await r.json();
    $("inverters").innerHTML = invs.length ? invs.map(inverterCard).join("") : '<div class="card muted">No inverter samples yet.</div>';
  } catch (e) { console.error("Error loading inverter telemetry:", e); }
}

async function updateSummary() {
  try {
    const r = await fetch("/api/summary");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = await r.json();
    const y = s.yesterday;
    $("pvSub").textContent = "today " + kwh(s.today.pv_wh) + " · yesterday " + (y ? kwh(y.pv_energy_wh) : "--");
    $("loadSub").textContent = "today " + kwh(s.today.load_wh) + " · yesterday " + (y ? kwh(y.load_energy_wh) : "--");
    $("socMinToday").textContent = fmt(s.today.battery_soc_min, 0, "%");
    $("socMinYesterday").textContent = "yesterday " + (y ? fmt(y.battery_soc_min, 0, "%") : "--");
    const pk = s.today.pv_peak_watts;
    $("pvPeak").textContent = isNum(pk) && pk > 0 ? watts(pk) : "--";
    $("pvPeakLabel").textContent = [
      isNum(pk) && pk > 0 && s.today.pv_peak_time ? "at " + timeText(s.today.pv_peak_time) : null,
      "yesterday " + (y && isNum(y.pv_peak_watts) ? watts(y.pv_peak_watts) : "--"),
    ].filter(Boolean).join(" · ");
    if (y && isNum(y.performance_ratio)) {
      $("prYesterday").textContent = (y.performance_ratio * 100).toFixed(0) + "%";
      $("prLabel").textContent = "of nameplate × " + (y.insolation_wh_m2 / 1000).toFixed(2) + " kWh/m² insolation";
    } else {
      $("prYesterday").textContent = "--";
      $("prLabel").textContent = !s.installed_pv_kw ? "set installed_pv_kw to enable"
        : !s.has_coordinates ? "set latitude and longitude to enable"
        : !y ? "no summary for yesterday yet" : "no insolation data for yesterday";
    }
    const b = s.battery;
    if (b) {
      if (b.state === "idle") { $("runtime").textContent = "idle"; $("runtimeLabel").textContent = "battery power below 25 W"; }
      else {
        $("runtime").textContent = b.hours > 99 ? "> 99 h" : b.hours + " h";
        $("runtimeLabel").textContent = b.state === "discharging" ? "to empty at current load" : "to full at current charge rate";
      }
    }
  } catch (e) { console.error("Error loading summary:", e); }
}

async function updateEvents() {
  try {
    const r = await fetch("/api/events?limit=25");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const events = await r.json();
    const body = $("events");
    if (!events.length) { body.innerHTML = '<tr><td class="muted">No events recorded.</td></tr>'; return; }
    body.innerHTML = events.map(ev => `<tr>
      <td class="time" title="${esc(new Date(ev.timestamp).toLocaleString())}">${esc(whenText(ev.timestamp))}</td>
      <td class="type"><span class="sev ${esc(ev.severity)}"></span>${esc(String(ev.event_type || "").replace(/_/g, " "))}</td>
      <td>${esc(ev.message)}${ev.inverter_id ? ` <span class="chip">inverter ${esc(ev.inverter_id)}</span>` : ""}</td></tr>`).join("");
  } catch (e) { console.error("Error loading events:", e); }
}

// ----------------------------------------------------------------- charts
function niceStep(range, target) {
  const raw = range / Math.max(1, target), mag = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * mag >= raw) return m * mag;
  return 10 * mag;
}
function axisFmt(v, unit, step) {
  if (unit === "W") return (step || 0) >= 1000 || Math.abs(v) >= 1000 ? (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + " kW" : Math.round(v) + " W";
  if (unit === "%") return Math.round(v) + "%";
  return v.toFixed((step || 1) >= 1 ? 0 : 1) + " " + unit;
}
const DAY = 86400000, HOUR = 3600000;
function timeTicks(minT, maxT, target) {
  const span = maxT - minT, out = [];
  const steps = [60000, 300000, 600000, 900000, 1800000, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR];
  const sub = steps.find(s => span / s <= target);
  if (sub) {
    const tz = t => new Date(t).getTimezoneOffset() * 60000;
    let t = Math.ceil((minT - tz(minT)) / sub) * sub + tz(minT);
    for (; t <= maxT; t += sub) {
      const d = new Date(t);
      const midnight = d.getHours() === 0 && d.getMinutes() === 0;
      out.push({t, label: midnight ? d.toLocaleDateString([], {month: "short", day: "numeric"})
                                   : d.toLocaleTimeString([], {hour: "numeric", minute: sub >= HOUR ? undefined : "2-digit"})});
    }
    return out;
  }
  const d = new Date(minT); d.setHours(0, 0, 0, 0);
  if (span / DAY <= target * 14) {
    const stepDays = [1, 2, 7, 14].find(s => span / DAY / s <= target) || 14;
    if (d.getTime() < minT) d.setDate(d.getDate() + 1);
    for (; d.getTime() <= maxT; d.setDate(d.getDate() + stepDays))
      out.push({t: d.getTime(), label: d.toLocaleDateString([], {month: "short", day: "numeric"})});
    return out;
  }
  d.setDate(1);
  const stepMonths = [1, 2, 3, 6, 12].find(s => span / (30.4 * DAY) / s <= target) || 12;
  if (d.getTime() < minT) d.setMonth(d.getMonth() + 1);
  for (; d.getTime() <= maxT; d.setMonth(d.getMonth() + stepMonths))
    out.push({t: d.getTime(), label: d.toLocaleDateString([], stepMonths >= 12 ? {year: "numeric"} :
      span > 400 * DAY || d.getMonth() === 0 ? {month: "short", year: "numeric"} : {month: "short"})});
  return out;
}

class Chart {
  constructor(canvasId, tipId, opts) {
    this.canvas = $(canvasId); this.tip = $(tipId); this.opts = opts;
    this.points = []; this.hover = null;
    this.canvas.addEventListener("pointermove", e => this.onMove(e));
    this.canvas.addEventListener("pointerleave", () => { this.hover = null; this.tip.style.display = "none"; this.draw(); });
    new ResizeObserver(() => this.draw()).observe(this.canvas);
  }
  setData(points, series, unit, mode, bucket) {
    this.points = points.filter(p => Number.isFinite(new Date(p.timestamp).getTime()));
    this.times = this.points.map(p => new Date(p.timestamp).getTime());
    this.series = series; this.unit = unit; this.mode = mode; this.bucket = bucket; this.hover = null;
    this.tip.style.display = "none";
    this.draw();
  }
  layout() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
      this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
    }
    const ctx = this.canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return {ctx, w, h};
  }
  draw() {
    const {ctx, w, h} = this.layout();
    ctx.clearRect(0, 0, w, h);
    ctx.font = "12px system-ui, sans-serif";
    const pts = this.points || [], series = this.series || [];
    const values = [];
    for (const s of series) for (const p of pts) for (const k of [s.key, ...(s.band || [])]) if (isNum(p[k])) values.push(Number(p[k]));
    if (!values.length) {
      ctx.fillStyle = css("--muted"); ctx.fillText("No historical data for this range.", 12, 24); return;
    }
    let lo = this.opts.yMin === "auto" ? Math.min(...values) : 0, hi = Math.max(...values);
    if (this.opts.yMax !== undefined) hi = Math.max(hi, this.opts.yMax);
    if (this.opts.yCap !== undefined) hi = Math.min(hi, this.opts.yCap);
    const step = niceStep(hi - lo || 1, 4);
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step; if (hi === lo) hi = lo + step;
    const yTicks = []; for (let v = lo; v <= hi + 1e-9; v += step) yTicks.push(v);
    const labels = yTicks.map(v => axisFmt(v, this.unit, step));
    const left = Math.ceil(Math.max(...labels.map(l => ctx.measureText(l).width))) + 12, right = 10, top = 10, bottom = 26;
    const pw = w - left - right, ph = h - top - bottom;
    const bars = this.mode === "bars" && pts.length <= 130;
    let minT = Math.min(...this.times), maxT = Math.max(...this.times);
    if (bars) {
      // Centre each bar on its bucket and keep the end bars inside the plot.
      const pad = (pts.length > 1 ? (maxT - minT) / (pts.length - 1) : DAY) / 2;
      minT -= pad; maxT += pad;
    }
    const span = Math.max(maxT - minT, 1);
    const X = t => left + (t - minT) / span * pw, Y = v => top + ph - (v - lo) / (hi - lo) * ph;

    ctx.strokeStyle = css("--line-2"); ctx.lineWidth = 1; ctx.fillStyle = css("--muted"); ctx.textBaseline = "middle";
    for (let i = 0; i < yTicks.length; i++) {
      const y = Math.round(Y(yTicks[i])) + 0.5;
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(labels[i], left - 8, y);
    }
    ctx.textBaseline = "alphabetic"; ctx.textAlign = "center";
    for (const tk of timeTicks(minT, maxT, Math.max(2, Math.floor(pw / 90)))) {
      const x = Math.round(X(tk.t)) + 0.5;
      ctx.strokeStyle = css("--line"); ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + ph); ctx.stroke();
      ctx.fillText(tk.label, x, h - 8);
    }

    const slot = pw / Math.max(1, pts.length);
    for (const s of series) {
      const color = css(s.color);
      if (bars && s.bars) {
        const bw = Math.min(24, Math.max(1, slot - 2));
        ctx.fillStyle = color;
        for (let i = 0; i < pts.length; i++) {
          if (!isNum(pts[i][s.key])) continue;
          const x = X(this.times[i]) - bw / 2, y = Y(Number(pts[i][s.key])), b = Y(lo), r = Math.min(3, bw / 2);
          ctx.beginPath(); ctx.moveTo(x, b); ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r);
          ctx.lineTo(x + bw - r, y); ctx.arcTo(x + bw, y, x + bw, y + r, r); ctx.lineTo(x + bw, b); ctx.fill();
        }
        continue;
      }
      if (s.band) {
        // Shaded min–max envelope, split at gaps.
        ctx.fillStyle = color; ctx.globalAlpha = 0.16; let seg = [];
        const flush = () => {
          if (seg.length > 1) {
            ctx.beginPath(); seg.forEach((q, j) => j ? ctx.lineTo(q.x, q.hi) : ctx.moveTo(q.x, q.hi));
            for (let j = seg.length - 1; j >= 0; j--) ctx.lineTo(seg[j].x, seg[j].lo);
            ctx.closePath(); ctx.fill();
          }
          seg = [];
        };
        for (let i = 0; i < pts.length; i++) {
          const a = pts[i][s.band[0]], b = pts[i][s.band[1]];
          if (!isNum(a) || !isNum(b)) { flush(); continue; }
          if (pts[i].gap_before) flush();
          seg.push({x: X(this.times[i]), lo: Y(Number(a)), hi: Y(Number(b))});
        }
        flush(); ctx.globalAlpha = 1;
      }
      if (s.fill) {
        ctx.fillStyle = color; ctx.globalAlpha = 0.12; ctx.beginPath(); let open = false, startX = 0;
        for (let i = 0; i < pts.length; i++) {
          const v = pts[i][s.key];
          if (!isNum(v) || pts[i].gap_before) { if (open) { ctx.lineTo(X(this.times[i - 1]), Y(lo)); ctx.lineTo(startX, Y(lo)); ctx.closePath(); ctx.fill(); ctx.beginPath(); open = false; } if (!isNum(v)) continue; }
          if (!open) { startX = X(this.times[i]); ctx.moveTo(startX, Y(lo)); open = true; }
          ctx.lineTo(X(this.times[i]), Y(Number(v)));
        }
        if (open) { ctx.lineTo(X(this.times[pts.length - 1]), Y(lo)); ctx.closePath(); ctx.fill(); }
        ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.beginPath();
      let pen = false;
      for (let i = 0; i < pts.length; i++) {
        const v = pts[i][s.key];
        if (!isNum(v)) { pen = false; continue; }
        const x = X(this.times[i]), y = Y(Number(v));
        if (!pen || pts[i].gap_before) { ctx.moveTo(x, y); pen = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    if (this.hover !== null && pts[this.hover]) {
      const i = this.hover, x = Math.round(X(this.times[i])) + 0.5;
      ctx.strokeStyle = css("--muted"); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + ph); ctx.stroke();
      for (const s of series) {
        if (!isNum(pts[i][s.key])) continue;
        ctx.beginPath(); ctx.arc(x, Y(Number(pts[i][s.key])), 4.5, 0, Math.PI * 2);
        ctx.fillStyle = css("--surface"); ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = css(s.color); ctx.stroke();
      }
    }
    this.geom = {left, pw, minT, span};
  }
  onMove(e) {
    if (!this.points.length || !this.geom) return;
    const rect = this.canvas.getBoundingClientRect();
    const t = this.geom.minT + (e.clientX - rect.left - this.geom.left) / this.geom.pw * this.geom.span;
    let best = 0, bd = Infinity;
    for (let i = 0; i < this.times.length; i++) { const d = Math.abs(this.times[i] - t); if (d < bd) { bd = d; best = i; } }
    if (best !== this.hover) { this.hover = best; this.draw(); }
    const p = this.points[best], d = new Date(this.times[best]);
    const tip = this.tip; tip.textContent = "";
    const head = document.createElement("div"); head.className = "t";
    head.textContent = !this.bucket ? d.toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})
      : this.bucket === "month" ? d.toLocaleDateString([], {month: "long", year: "numeric"})
      : (this.bucket === "week" ? "Week of " : "") + d.toLocaleDateString([], {weekday: this.bucket === "week" ? undefined : "short", month: "short", day: "numeric", year: "numeric"});
    tip.appendChild(head);
    for (const s of this.series) {
      const row = document.createElement("div"); row.className = "r";
      const sw = document.createElement("i"); sw.style.background = css(s.color);
      const val = v => !isNum(v) ? "--" : this.unit === "W" ? watts(Number(v)) : this.unit === "kWh" ? Number(v).toFixed(1) + " kWh" : axisFmt(Number(v), this.unit);
      const b = document.createElement("b"); b.textContent = val(p[s.key]);
      const l = document.createElement("span"); l.textContent = s.label;
      row.append(sw, b, l); tip.appendChild(row);
      if (s.band && isNum(p[s.band[1]])) {
        const r2 = document.createElement("div"); r2.className = "r";
        const sw2 = document.createElement("i"); sw2.style.background = css(s.color); sw2.style.opacity = 0.4;
        const b2 = document.createElement("b"); b2.textContent = val(p[s.band[1]]);
        const l2 = document.createElement("span"); l2.textContent = s.bandLabel || "highest";
        r2.append(sw2, b2, l2); tip.appendChild(r2);
      }
    }
    tip.style.display = "block";
    const cx = e.clientX - rect.left, cardRect = this.canvas.parentElement.getBoundingClientRect();
    const tw = tip.offsetWidth, baseX = this.canvas.offsetLeft + cx;
    tip.style.left = (baseX + 14 + tw > cardRect.width - 8 ? baseX - 14 - tw : baseX + 14) + "px";
    tip.style.top = (this.canvas.offsetTop + 8) + "px";
  }
}

const powerChart = new Chart("powerChart", "powerTip", {yMin: 0});
const socChart = new Chart("socChart", "socTip", {yMin: "auto", yMax: 100, yCap: 100});

async function updateHistoryCharts() {
  try {
    const r = await fetch("/api/history?hours=" + historyHours);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const payload = await r.json();
    const points = payload.points || [];
    const energy = payload.mode === "daily_energy", bucket = payload.bucket || "day";
    $("powerTitle").textContent = !energy ? "Power" : bucket === "day" ? "Energy per day"
      : "Energy per day, " + (bucket === "week" ? "weekly" : "monthly") + " average";
    $("pvLegend").textContent = energy ? "Solar kWh" : "Solar";
    $("loadLegend").textContent = energy ? "Load kWh" : "Load";
    powerChart.setData(points, [
      {key: "pv_value", color: "--solar", label: "solar", fill: true, bars: true},
      {key: "load_value", color: "--load", label: "load"},
    ], energy ? "kWh" : "W", energy ? "bars" : "line", energy ? bucket : undefined);
    // Daily buckets carry a min/max: show the lowest SOC with each day's swing
    // shaded, which is what matters on an off-grid system.
    const ranged = energy && points.some(p => isNum(p.battery_soc_min) && isNum(p.battery_soc_max));
    $("socLegend").textContent = ranged ? "line: lowest · shaded: range" : "";
    socChart.setData(points, ranged
      ? [{key: "battery_soc_min", color: "--soc", label: "lowest", band: ["battery_soc_min", "battery_soc_max"]}]
      : [{key: "battery_soc", color: "--soc", label: "SOC", fill: true}], "%", "line", energy ? bucket : undefined);
  } catch (e) { console.error("Error loading history:", e); }
}

function setHistoryRange(hours) {
  historyHours = hours;
  $("historyTitle").textContent = LABELS[hours] || "History";
  document.querySelectorAll("#ranges button").forEach(b => b.classList.toggle("active", Number(b.dataset.hours) === hours));
  try { localStorage.setItem("solar.range", hours); } catch (e) {}
  updateHistoryCharts();
}
document.querySelectorAll("#ranges button").forEach(b => b.addEventListener("click", () => setHistoryRange(Number(b.dataset.hours))));
try { const saved = Number(localStorage.getItem("solar.range")); if (LABELS[saved]) { historyHours = saved; } } catch (e) {}
$("historyTitle").textContent = LABELS[historyHours];
document.querySelectorAll("#ranges button").forEach(b => b.classList.toggle("active", Number(b.dataset.hours) === historyHours));

// Refresh only while the tab is visible so idle phones do not load the Pi.
const every = (fn, ms) => setInterval(() => { if (!document.hidden) fn(); }, ms);
function refreshAll() { updateDashboard(); updateSummary(); updateInverters(); updateEvents(); updateHistoryCharts(); }
refreshAll();
every(updateDashboard, 10000); every(updateInverters, 10000);
every(updateSummary, 30000); every(updateEvents, 30000); every(updateHistoryCharts, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
</script>
</body>
</html>
""".replace("__BASE_CSS__", BASE_CSS)


DAYS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Solar Monitor · Daily</title>
<style>
__BASE_CSS__
  .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 0 0 14px; }
  .controls label { color: var(--muted); font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 14px; }
  .tile { background: var(--surface); border-radius: 10px; padding: 12px 14px; }
  .tile .label { color: var(--muted); font-size: 13px; }
  .tile .value { margin-top: 4px; font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .tile .sub { margin-top: 2px; font-size: 12px; color: var(--muted); }
  .wrap { overflow: auto; max-height: calc(100vh - 250px); min-height: 320px; background: var(--surface); border-radius: 10px; }
  table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 13.5px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: right; }
  th { color: var(--muted); font-weight: 500; position: sticky; top: 0; background: var(--surface); z-index: 1; font-size: 12.5px; }
  th.group { text-align: center; border-bottom: 0; padding-bottom: 0; color: var(--faint); text-transform: uppercase; letter-spacing: 0.06em; font-size: 11px; }
  thead tr:nth-child(2) th { top: 24px; }
  td:first-child, th:first-child { text-align: left; position: sticky; left: 0; background: var(--surface); }
  thead th:first-child { z-index: 2; }
  tbody tr:hover td { background: var(--surface-2); }
  td .wd { color: var(--muted); font-size: 12px; margin-left: 6px; }
  .bar { display: inline-block; height: 7px; width: 64px; background: var(--surface-2); border-radius: 3px; vertical-align: middle; margin-left: 8px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--solar); border-radius: 3px; }
  .low { color: var(--bad); }
  .dim { color: var(--faint); }
  @media (max-width: 600px) { body { padding: 0 12px 24px; } .bar { display: none; } }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">Solar Monitor</div>
  <nav><a href="/">Live</a><a href="/days" class="active">Daily table</a></nav>
</header>
<div class="controls">
  <label>Range <select id="range">
    <option value="30">30 days</option><option value="90" selected>90 days</option>
    <option value="365">1 year</option><option value="3660">All</option></select></label>
  <a class="btn" id="csv" href="/api/days.csv?days=90">Download CSV</a>
  <span class="muted" id="count"></span>
</div>
<div class="tiles" id="totals"></div>
<div class="wrap"><table>
<thead>
<tr><th class="group"></th><th class="group" colspan="4">Energy, kWh</th><th class="group" colspan="2">Battery, kWh</th>
    <th class="group" colspan="2">Production</th><th class="group" colspan="2">SOC</th><th class="group" colspan="2">Battery use</th><th class="group" colspan="3">Performance</th></tr>
<tr>
  <th>Date</th><th>Solar</th><th>Load</th><th>Inv 1</th><th>Inv 2</th>
  <th>Charge</th><th>Discharge</th><th>Peak W</th><th>Hours</th>
  <th>SOC min</th><th>SOC max</th><th>RTE</th><th>Cycles</th><th>Yield kWh/kW</th>
  <th>Insolation kWh/m²</th><th>PR</th>
</tr></thead>
<tbody id="rows"><tr><td colspan="16" class="muted">Loading…</td></tr></tbody>
</table></div>
<script>
"use strict";
const k = v => v == null ? "--" : (v / 1000).toFixed(1);
const n = (v, d, s) => v == null ? "--" : Number(v).toFixed(d) + (s || "");
const pct = v => v == null ? "--" : (v * 100).toFixed(0) + "%";
const dim = s => s === "--" ? '<span class="dim">--</span>' : s;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const weekday = iso => new Date(iso + "T12:00:00").toLocaleDateString([], {weekday: "short"});
const avg = xs => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
function tile(label, value, sub) {
  return `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub || ""}</div></div>`;
}
async function load() {
  const days = document.getElementById("range").value;
  document.getElementById("csv").href = "/api/days.csv?days=" + days;
  const r = await fetch("/api/days?days=" + days);
  const rows = await r.json();
  const maxPv = Math.max(1, ...rows.map(x => x.pv_energy_wh || 0));
  const pv = rows.map(x => x.pv_energy_wh).filter(v => v != null), ld = rows.map(x => x.load_energy_wh).filter(v => v != null);
  const best = rows.reduce((b, x) => (x.pv_energy_wh || 0) > (b?.pv_energy_wh || 0) ? x : b, null);
  const prs = rows.map(x => x.performance_ratio).filter(v => v != null), socs = rows.map(x => x.battery_soc_min).filter(v => v != null);
  document.getElementById("count").textContent = rows.length ? rows.length + " completed days" : "";
  document.getElementById("totals").innerHTML = rows.length ? [
    tile("Solar total", k(pv.reduce((a, b) => a + b, 0)) + " kWh", "avg " + k(avg(pv)) + " kWh/day"),
    tile("Load total", k(ld.reduce((a, b) => a + b, 0)) + " kWh", "avg " + k(avg(ld)) + " kWh/day"),
    tile("Best day", best ? k(best.pv_energy_wh) + " kWh" : "--", best ? best.date : ""),
    tile("Lowest SOC", socs.length ? Math.min(...socs).toFixed(0) + "%" : "--",
         socs.length ? rows.find(x => x.battery_soc_min === Math.min(...socs)).date : "across the range"),
    tile("Avg performance ratio", prs.length ? pct(avg(prs)) : "--", prs.length ? prs.length + " days with insolation" : "needs weather data"),
  ].join("") : "";
  document.getElementById("rows").innerHTML = rows.length ? rows.map(x => `<tr>
    <td>${esc(x.date)}<span class="wd">${weekday(x.date)}</span></td>
    <td>${k(x.pv_energy_wh)}<span class="bar"><i style="width:${Math.round(100 * (x.pv_energy_wh || 0) / maxPv)}%"></i></span></td>
    <td>${k(x.load_energy_wh)}</td><td>${dim(k(x.inverter_1_pv_wh))}</td><td>${dim(k(x.inverter_2_pv_wh))}</td>
    <td>${dim(k(x.battery_charge_wh))}</td><td>${dim(k(x.battery_discharge_wh))}</td>
    <td>${dim(n(x.pv_peak_watts, 0))}</td><td>${x.production_minutes == null ? dim("--") : (x.production_minutes / 60).toFixed(1)}</td>
    <td class="${x.battery_soc_min != null && x.battery_soc_min < 30 ? "low" : ""}">${dim(n(x.battery_soc_min, 0, "%"))}</td>
    <td>${dim(n(x.battery_soc_max, 0, "%"))}</td>
    <td>${dim(pct(x.battery_round_trip_efficiency))}</td><td>${dim(n(x.equivalent_battery_cycles, 2))}</td>
    <td>${dim(n(x.specific_yield_kwh_per_kw, 2))}</td><td>${dim(k(x.insolation_wh_m2))}</td><td>${dim(pct(x.performance_ratio))}</td>
  </tr>`).join("") : '<tr><td colspan="16" class="muted">No completed days yet.</td></tr>';
}
document.getElementById("range").addEventListener("change", load);
load();
</script>
</body>
</html>
""".replace("__BASE_CSS__", BASE_CSS)


if __name__ == "__main__":
    app.run(host=settings.DASHBOARD_HOST, port=settings.DASHBOARD_PORT, debug=False)
