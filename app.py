"""Flask dashboard and JSON API."""

import json
import sqlite3
from datetime import timedelta

from flask import Flask, jsonify, render_template_string, request

import settings
from eg4 import MODES
from timeutil import now_utc, parse_iso, to_iso, today_local

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
</style>
</head>
<body>

<h1>Solar Monitor</h1>
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
    <div class="card"><div class="label">PV Voltage</div><div class="small-value">${fmt(inv.pv_voltage, 1, " V")}</div></div>
    <div class="card"><div class="label">Output Voltage</div><div class="small-value">${fmt(inv.output_voltage, 1, " V")}</div></div>
    <div class="card"><div class="label">Battery Voltage</div><div class="small-value">${fmt(inv.battery_voltage, 1, " V")}</div></div>
    <div class="card"><div class="label">Temperature</div><div class="small-value">${fmt(inv.inverter_temperature_c, 0, " °C")}</div></div>
  </div>`;
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

updateDashboard(); updateInverters(); updateEvents(); updateHistoryCharts();
setInterval(updateDashboard, 10000);
setInterval(updateInverters, 10000);
setInterval(updateEvents, 30000);
setInterval(updateHistoryCharts, 60000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host=settings.DASHBOARD_HOST, port=settings.DASHBOARD_PORT, debug=False)
