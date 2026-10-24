"""Flask dashboard and JSON API."""

import csv
import io
import json
import sqlite3
from datetime import timedelta

from flask import Flask, Response, jsonify, render_template_string, request

import settings
from aggregator import integrate_energy
from eg4 import MODES
from timeutil import local_day_bounds, now_utc, parse_iso, to_iso, today_local

app = Flask(__name__)

MAX_HISTORY_HOURS = 24 * 365 * 3


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
        finally:
            conn.close()
        if row is None:
            return None
        reading = dict(row)

    try:
        age = (now_utc() - parse_iso(reading["timestamp"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = None
    reading["age_seconds"] = None if age is None else int(age)
    reading["stale"] = age is None or age > settings.STALE_AFTER_SECONDS
    return reading


def get_latest_inverters():
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            SELECT s.timestamp, s.inverter_id, s.output_voltage, s.output_frequency,
                   s.output_watts, s.output_va, s.load_percent, s.battery_voltage,
                   s.battery_soc, s.pv_voltage, s.pv_current, s.pv_watts,
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
            FROM events ORDER BY id DESC LIMIT ?
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
            "insolation_wh_m2, performance_ratio FROM daily_summary WHERE date = ?",
            ((today_local() - timedelta(days=1)).isoformat(),),
        ).fetchone()
        today = energy_today(conn)
        soc_min_today = conn.execute(
            "SELECT MIN(battery_soc) FROM system_samples WHERE timestamp >= ?",
            (to_iso(local_day_bounds(today_local())[0]),),
        ).fetchone()[0]
    finally:
        conn.close()

    reading = get_latest_reading()
    capacity = float(config.get("battery_capacity_kwh") or 0)
    return {
        "today": dict(today, battery_soc_min=soc_min_today),
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
                       AVG((battery_soc_min + battery_soc_max) / 2.0) AS battery_soc
                FROM quarter_hour_summary
                WHERE timestamp >= ?
                {_bucket_group(bucket_seconds)}
                ORDER BY MIN(timestamp)
                """,
                (to_iso(now - timedelta(hours=hours)),),
            ).fetchall()
            return _history("quarter_hour", rows, max(2700, bucket_seconds * 3))

        days = max(1, int(round(hours / 24)))
        bucket_days = max(1, -(-days // max_points))
        rows = conn.execute(
            f"""
            SELECT MIN(date) || 'T12:00:00' AS timestamp,
                   AVG(pv_energy_wh) / 1000.0 AS pv_value,
                   AVG(load_energy_wh) / 1000.0 AS load_value,
                   AVG((battery_soc_min + battery_soc_max) / 2.0) AS battery_soc
            FROM daily_summary
            WHERE date >= ?
            GROUP BY CAST(julianday(date) AS INTEGER) / {bucket_days}
            ORDER BY MIN(date)
            """,
            ((today_local() - timedelta(days=days)).isoformat(),),
        ).fetchall()
        return {
            "mode": "daily_energy",
            "source": "daily",
            "pv_unit": "kWh/day",
            "load_unit": "kWh/day",
            "points": add_gap_markers(rows, bucket_days * 86400),
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


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solar Monitor</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 20px; background: #111; color: #eee; font-family: Arial, sans-serif; }
  h1 { margin-top: 0; }
  .status { margin-bottom: 18px; color: #aaa; }
  .banner { display: none; margin-bottom: 18px; padding: 12px 16px; border-radius: 8px;
            background: #5a2a00; color: #ffd9b3; border: 1px solid #b35900; }
  .banner.fault { background: #5a0000; color: #ffc9c9; border-color: #b30000; }
  .banner.show { display: block; }
  .section-title { margin-top: 32px; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .card { background: #1b1b1b; border-radius: 10px; padding: 16px; }
  .label { color: #aaa; font-size: 14px; }
  .value { margin-top: 8px; font-size: 28px; font-weight: bold; }
  .small-value { margin-top: 8px; font-size: 22px; font-weight: bold; }
  .quality-label { margin-top: 6px; font-size: 13px; opacity: 0.75; }
  .fault { color: #ff6b6b; }
  .history-controls { display: flex; gap: 8px; margin: 10px 0 16px 0; flex-wrap: wrap; }
  .history-range-button { border: 1px solid #555; background: #222; color: #ddd;
                          padding: 7px 12px; border-radius: 7px; cursor: pointer; }
  .history-range-button.active { background: #444; border-color: #888; }
  .chart-card { margin-top: 14px; padding: 16px; background: #1b1b1b; border-radius: 10px; }
  .chart-card canvas { width: 100%; height: 260px; display: block; }
  table.events { width: 100%; border-collapse: collapse; font-size: 14px; }
  table.events td { padding: 6px 8px; border-bottom: 1px solid #2a2a2a; vertical-align: top; }
  table.events td.time { color: #aaa; white-space: nowrap; }
  .sev-info { color: #9ecbff; } .sev-warning { color: #ffc966; } .sev-error { color: #ff6b6b; }
  a.nav { font-size: 14px; font-weight: normal; margin-left: 16px; color: #9ecbff; }
</style>
</head>
<body>

<h1>Solar Monitor <a class="nav" href="/days">Daily table</a></h1>
<div class="status" id="status">Waiting for telemetry...</div>
<div class="banner" id="banner"></div>

<h2 class="section-title">System</h2>
<div class="grid">
  <div class="card"><div class="label">Solar Production</div><div class="value" id="pvWatts">--</div></div>
  <div class="card"><div class="label">House Load</div><div class="value" id="loadWatts">--</div></div>
  <div class="card"><div class="label">Battery SOC</div><div class="value" id="batterySoc">--</div></div>
  <div class="card"><div class="label">Battery Voltage</div><div class="value" id="batteryVoltage">--</div></div>
  <div class="card"><div class="label">Battery Power</div><div class="value" id="batteryWatts">--</div>
       <div class="quality-label">+ charging / - discharging</div></div>
  <div class="card"><div class="label">Battery Runtime</div><div class="value" id="runtime">--</div>
       <div class="quality-label" id="runtimeLabel">set battery_capacity_kwh to enable</div></div>
  <div class="card"><div class="label">Utility / AC Input</div><div class="value" id="grid">--</div>
       <div class="quality-label" id="gridLabel"></div></div>
</div>

<h2 class="section-title">Energy</h2>
<div class="grid">
  <div class="card"><div class="label">Solar Today</div><div class="value" id="pvToday">--</div>
       <div class="quality-label" id="pvYesterday">yesterday --</div></div>
  <div class="card"><div class="label">Load Today</div><div class="value" id="loadToday">--</div>
       <div class="quality-label" id="loadYesterday">yesterday --</div></div>
  <div class="card"><div class="label">Lowest SOC Today</div><div class="value" id="socMinToday">--</div>
       <div class="quality-label" id="socMinYesterday">yesterday --</div></div>
  <div class="card"><div class="label">Yesterday's Performance</div><div class="value" id="prYesterday">--</div>
       <div class="quality-label" id="prLabel">needs latitude/longitude and installed_pv_kw</div></div>
</div>

<div id="inverters"></div>

<h2 class="section-title" id="historyTitle">Last 24 Hours</h2>
<div class="history-controls">
  <button class="history-range-button" data-hours="1">1h</button>
  <button class="history-range-button" data-hours="6">6h</button>
  <button class="history-range-button active" data-hours="24">24h</button>
  <button class="history-range-button" data-hours="168">7d</button>
  <button class="history-range-button" data-hours="336">2w</button>
  <button class="history-range-button" data-hours="720">1m</button>
  <button class="history-range-button" data-hours="2160">3m</button>
  <button class="history-range-button" data-hours="4320">6m</button>
  <button class="history-range-button" data-hours="6480">9m</button>
  <button class="history-range-button" data-hours="8760">1y</button>
  <button class="history-range-button" data-hours="17520">2y</button>
  <button class="history-range-button" data-hours="26280">3y</button>
</div>

<div class="chart-card"><div class="label" id="pvHistoryLabel">Solar Production</div>
  <canvas id="pvChart" width="900" height="260"></canvas></div>
<div class="chart-card"><div class="label" id="loadHistoryLabel">House Load</div>
  <canvas id="loadChart" width="900" height="260"></canvas></div>
<div class="chart-card"><div class="label">Battery SOC</div>
  <canvas id="socChart" width="900" height="260"></canvas></div>

<h2 class="section-title">Recent Events</h2>
<div class="card"><table class="events"><tbody id="events"><tr><td>No events recorded.</td></tr></tbody></table></div>

<script>
let historyHours = 24;

const LABELS = {1:"Last Hour",6:"Last 6 Hours",24:"Last 24 Hours",168:"Last 7 Days",
  336:"Last 2 Weeks",720:"Last Month",2160:"Last 3 Months",4320:"Last 6 Months",
  6480:"Last 9 Months",8760:"Last Year",17520:"Last 2 Years",26280:"Last 3 Years"};

const fmt = (v, d, unit) => (v === null || v === undefined || Number.isNaN(Number(v)))
  ? "--" : Number(v).toFixed(d) + unit;
const localTime = iso => { const d = new Date(iso); return Number.isFinite(d.getTime()) ? d.toLocaleString() : iso; };
const ageText = s => s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h";

async function updateDashboard() {
  const banner = document.getElementById("banner");
  try {
    const r = await fetch("/api/latest");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    document.getElementById("pvWatts").textContent = fmt(d.pv_watts, 0, " W");
    document.getElementById("loadWatts").textContent = fmt(d.load_watts, 0, " W");
    document.getElementById("batterySoc").textContent = fmt(d.battery_soc, 0, "%");
    document.getElementById("batteryVoltage").textContent = fmt(d.battery_voltage, 1, " V");
    document.getElementById("batteryWatts").textContent = fmt(d.battery_watts, 0, " W");
    if (d.grid_present === true) {
      document.getElementById("grid").textContent = "On";
      document.getElementById("gridLabel").textContent = fmt(d.grid_voltage, 0, " V") + " at the input";
    } else if (d.grid_present === false) {
      document.getElementById("grid").textContent = "Off";
      document.getElementById("gridLabel").textContent = "running on solar and battery";
    }
    document.getElementById("status").textContent =
      "Last update: " + localTime(d.timestamp) + " | " + d.inverter_count + " inverters | " + d.source;
    banner.className = "banner";
    if (d.stale) {
      banner.textContent = "Telemetry is stale: last reading was " + ageText(d.age_seconds) + " ago. The collector may be down.";
      banner.classList.add("show");
    } else if (d.any_fault) {
      banner.textContent = "An inverter is reporting a fault. See inverter cards and events.";
      banner.classList.add("show", "fault");
    }
  } catch (e) {
    document.getElementById("status").textContent = "Telemetry unavailable: " + e;
  }
}

function inverterCard(inv) {
  const faulted = inv.fault_code && !["", "00", "0"].includes(inv.fault_code);
  return `
  <h2 class="section-title">Inverter ${inv.inverter_id}</h2>
  <div class="grid">
    <div class="card"><div class="label">Mode</div>
      <div class="small-value">${inv.mode_name || "--"}</div>
      <div class="quality-label">code ${inv.mode || "--"}</div></div>
    <div class="card"><div class="label">Fault</div>
      <div class="small-value ${faulted ? "fault" : ""}">${faulted ? "Code " + inv.fault_code : "None"}</div></div>
    <div class="card"><div class="label">Output Power</div><div class="small-value">${fmt(inv.output_watts, 0, " W")}</div></div>
    <div class="card"><div class="label">Load</div><div class="small-value">${fmt(inv.load_percent, 0, "%")}</div></div>
    <div class="card"><div class="label">PV Power</div><div class="small-value">${fmt(inv.pv_watts, 0, " W")}</div>
      <div class="quality-label">${inv.pv_quality}</div></div>
    <div class="card"><div class="label">PV1</div><div class="small-value">${fmt(inv.pv1_watts, 0, " W")}</div>
      <div class="quality-label">${fmt(inv.pv1_voltage, 0, " V")} · ${fmt(inv.pv1_current, 1, " A")}</div></div>
    <div class="card"><div class="label">PV2</div><div class="small-value">${fmt(inv.pv2_watts, 0, " W")}</div>
      <div class="quality-label">${fmt(inv.pv2_voltage, 0, " V")} · ${fmt(inv.pv2_current, 1, " A")}</div></div>
    <div class="card"><div class="label">Output Voltage</div><div class="small-value">${fmt(inv.output_voltage, 1, " V")}</div></div>
    <div class="card"><div class="label">Battery Voltage</div><div class="small-value">${fmt(inv.battery_voltage, 1, " V")}</div></div>
    <div class="card"><div class="label">Temperature</div><div class="small-value">${fmt(inv.inverter_temperature_c, 0, " °C")}</div></div>
  </div>`;
}

const kwh = wh => (wh === null || wh === undefined) ? "--" : (wh / 1000).toFixed(1) + " kWh";

async function updateSummary() {
  try {
    const r = await fetch("/api/summary");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = await r.json();
    document.getElementById("pvToday").textContent = kwh(s.today.pv_wh);
    document.getElementById("loadToday").textContent = kwh(s.today.load_wh);
    document.getElementById("socMinToday").textContent = fmt(s.today.battery_soc_min, 0, "%");
    const y = s.yesterday;
    document.getElementById("pvYesterday").textContent = "yesterday " + (y ? kwh(y.pv_energy_wh) : "--");
    document.getElementById("loadYesterday").textContent = "yesterday " + (y ? kwh(y.load_energy_wh) : "--");
    document.getElementById("socMinYesterday").textContent = "yesterday " + (y ? fmt(y.battery_soc_min, 0, "%") : "--");
    if (y && y.performance_ratio !== null && y.performance_ratio !== undefined) {
      document.getElementById("prYesterday").textContent = (y.performance_ratio * 100).toFixed(0) + "%";
      document.getElementById("prLabel").textContent = "of nameplate × " + (y.insolation_wh_m2 / 1000).toFixed(2) + " kWh/m² insolation";
    }
    const b = s.battery;
    if (b) {
      if (b.state === "idle") {
        document.getElementById("runtime").textContent = "idle";
        document.getElementById("runtimeLabel").textContent = "battery power below 25 W";
      } else {
        document.getElementById("runtime").textContent = b.hours > 99 ? "> 99 h" : b.hours + " h";
        document.getElementById("runtimeLabel").textContent =
          b.state === "discharging" ? "to empty at current load" : "to full at current charge rate";
      }
    }
  } catch (e) { console.error("Error loading summary:", e); }
}

async function updateInverters() {
  try {
    const r = await fetch("/api/inverters");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const invs = await r.json();
    document.getElementById("inverters").innerHTML = invs.map(inverterCard).join("");
  } catch (e) { console.error("Error loading inverter telemetry:", e); }
}

async function updateEvents() {
  try {
    const r = await fetch("/api/events?limit=25");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const events = await r.json();
    const body = document.getElementById("events");
    if (!events.length) { body.innerHTML = "<tr><td>No events recorded.</td></tr>"; return; }
    body.innerHTML = events.map(ev => `<tr>
      <td class="time">${localTime(ev.timestamp)}</td>
      <td class="sev-${ev.severity}">${ev.severity}</td>
      <td>${ev.event_type}</td>
      <td>${ev.message}</td></tr>`).join("");
  } catch (e) { console.error("Error loading events:", e); }
}

function drawLineChart(canvasId, points, valueKey, unit) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const width = canvas.width, height = canvas.height;
  const left = 72, right = 16, top = 16, bottom = 38;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  ctx.clearRect(0, 0, width, height);

  const valid = points.filter(p => Number.isFinite(Number(p[valueKey])) && Number.isFinite(new Date(p.timestamp).getTime()));
  if (!valid.length) {
    ctx.fillStyle = "#aaa"; ctx.font = "14px sans-serif";
    ctx.fillText("No historical data for this range.", left, top + 20);
    return;
  }
  const values = valid.map(p => Number(p[valueKey]));
  const times = valid.map(p => new Date(p.timestamp).getTime());
  let minValue = valueKey === "battery_soc" ? Math.min(...values) : 0;
  let maxValue = Math.max(...values);
  if (maxValue === minValue) maxValue = minValue + 1;
  const minTime = Math.min(...times), maxTime = Math.max(...times);
  const timeSpan = Math.max(maxTime - minTime, 1);

  ctx.strokeStyle = "#555"; ctx.fillStyle = "#aaa"; ctx.lineWidth = 1; ctx.font = "12px sans-serif";
  const decimals = unit.includes("kWh") ? 1 : 0;
  for (let i = 0; i <= 4; i++) {
    const y = top + plotHeight * i / 4;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - right, y); ctx.stroke();
    ctx.fillText((maxValue - (maxValue - minValue) * i / 4).toFixed(decimals) + " " + unit, 4, y + 4);
  }

  ctx.strokeStyle = "#ddd"; ctx.lineWidth = 2; ctx.beginPath();
  let penDown = false;
  for (const p of valid) {
    const x = left + ((new Date(p.timestamp).getTime() - minTime) / timeSpan) * plotWidth;
    const y = top + plotHeight - ((Number(p[valueKey]) - minValue) / (maxValue - minValue)) * plotHeight;
    if (!penDown || p.gap_before) { ctx.moveTo(x, y); penDown = true; } else { ctx.lineTo(x, y); }
  }
  ctx.stroke();

  const opts = historyHours <= 24 ? {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}
             : historyHours <= 744 ? {month:"short", day:"numeric"}
             : {month:"short", day:"numeric", year:"numeric"};
  const firstLabel = new Date(valid[0].timestamp).toLocaleString([], opts);
  const lastLabel = new Date(valid[valid.length - 1].timestamp).toLocaleString([], opts);
  ctx.fillStyle = "#aaa";
  ctx.fillText(firstLabel, left, height - 9);
  ctx.fillText(lastLabel, width - right - ctx.measureText(lastLabel).width, height - 9);
}

async function updateHistoryCharts() {
  try {
    const r = await fetch("/api/history?hours=" + historyHours);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const payload = await r.json();
    const points = payload.points || [];
    const energyMode = payload.mode === "daily_energy";
    document.getElementById("pvHistoryLabel").textContent = energyMode ? "Solar Energy per Day" : "Solar Production";
    document.getElementById("loadHistoryLabel").textContent = energyMode ? "House Energy per Day" : "House Load";
    drawLineChart("pvChart", points, "pv_value", payload.pv_unit || "W");
    drawLineChart("loadChart", points, "load_value", payload.load_unit || "W");
    drawLineChart("socChart", points, "battery_soc", "%");
  } catch (e) { console.error("Error loading history:", e); }
}

function setHistoryRange(hours) {
  historyHours = hours;
  document.getElementById("historyTitle").textContent = LABELS[hours] || "History";
  document.querySelectorAll(".history-range-button").forEach(b =>
    b.classList.toggle("active", Number(b.dataset.hours) === hours));
  updateHistoryCharts();
}
document.querySelectorAll(".history-range-button").forEach(b =>
  b.addEventListener("click", () => setHistoryRange(Number(b.dataset.hours))));

updateDashboard(); updateSummary(); updateInverters(); updateEvents(); updateHistoryCharts();
setInterval(updateDashboard, 10000);
setInterval(updateSummary, 30000);
setInterval(updateInverters, 10000);
setInterval(updateEvents, 30000);
setInterval(updateHistoryCharts, 60000);
</script>
</body>
</html>
"""


DAYS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solar Monitor - Daily</title>
<style>
  body { margin: 0; padding: 20px; background: #111; color: #eee; font-family: Arial, sans-serif; }
  h1 { margin-top: 0; }
  a { color: #9ecbff; }
  .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
  button, select { border: 1px solid #555; background: #222; color: #ddd; padding: 7px 12px; border-radius: 7px; cursor: pointer; }
  .wrap { overflow-x: auto; background: #1b1b1b; border-radius: 10px; padding: 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; white-space: nowrap; }
  th, td { padding: 6px 10px; border-bottom: 1px solid #2a2a2a; text-align: right; }
  th { color: #aaa; font-weight: normal; position: sticky; top: 0; background: #1b1b1b; }
  td:first-child, th:first-child { text-align: left; }
  .bar { display: inline-block; height: 8px; background: #f0b429; border-radius: 4px; vertical-align: middle; margin-left: 6px; }
</style>
</head>
<body>
<h1>Daily Summary <a href="/" style="font-size:14px;font-weight:normal;margin-left:16px">Live dashboard</a></h1>
<div class="controls">
  <label>Range <select id="range">
    <option value="30">30 days</option><option value="90" selected>90 days</option>
    <option value="365">1 year</option><option value="3660">All</option></select></label>
  <a id="csv" href="/api/days.csv?days=90"><button>Download CSV</button></a>
</div>
<div class="wrap"><table>
<thead><tr>
  <th>Date</th><th>Solar kWh</th><th>Load kWh</th><th>Inv 1 kWh</th><th>Inv 2 kWh</th>
  <th>Charge kWh</th><th>Discharge kWh</th><th>Peak PV W</th><th>Production h</th>
  <th>SOC min</th><th>SOC max</th><th>RTE</th><th>Cycles</th><th>Yield kWh/kW</th>
  <th>Insolation kWh/m²</th><th>PR</th>
</tr></thead>
<tbody id="rows"><tr><td>Loading...</td></tr></tbody>
</table></div>
<script>
const k = v => v == null ? "--" : (v / 1000).toFixed(1);
const n = (v, d, s) => v == null ? "--" : Number(v).toFixed(d) + (s || "");
const pct = v => v == null ? "--" : (v * 100).toFixed(0) + "%";
async function load() {
  const days = document.getElementById("range").value;
  document.getElementById("csv").href = "/api/days.csv?days=" + days;
  const r = await fetch("/api/days?days=" + days);
  const rows = await r.json();
  const maxPv = Math.max(1, ...rows.map(x => x.pv_energy_wh || 0));
  document.getElementById("rows").innerHTML = rows.length ? rows.map(x => `<tr>
    <td>${x.date}</td>
    <td>${k(x.pv_energy_wh)}<span class="bar" style="width:${Math.round(60 * (x.pv_energy_wh || 0) / maxPv)}px"></span></td>
    <td>${k(x.load_energy_wh)}</td><td>${k(x.inverter_1_pv_wh)}</td><td>${k(x.inverter_2_pv_wh)}</td>
    <td>${k(x.battery_charge_wh)}</td><td>${k(x.battery_discharge_wh)}</td>
    <td>${n(x.pv_peak_watts, 0)}</td><td>${x.production_minutes == null ? "--" : (x.production_minutes / 60).toFixed(1)}</td>
    <td>${n(x.battery_soc_min, 0, "%")}</td><td>${n(x.battery_soc_max, 0, "%")}</td>
    <td>${pct(x.battery_round_trip_efficiency)}</td><td>${n(x.equivalent_battery_cycles, 2)}</td>
    <td>${n(x.specific_yield_kwh_per_kw, 2)}</td><td>${k(x.insolation_wh_m2)}</td><td>${pct(x.performance_ratio)}</td>
  </tr>`).join("") : "<tr><td>No completed days yet.</td></tr>";
}
document.getElementById("range").addEventListener("change", load);
load();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host=settings.DASHBOARD_HOST, port=settings.DASHBOARD_PORT, debug=False)
