from flask import Flask, jsonify, render_template_string
import json
import sqlite3
from pathlib import Path

DB_PATH = "solar_v1.db"
LIVE_PATH = Path("/run/solar-monitor/latest.json")

app = Flask(__name__)


def get_latest_reading():
    try:
        with LIVE_PATH.open("r") as file:
            return json.load(file)

    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Fallback to SQLite if the collector is not currently running.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute("""
        SELECT
            timestamp,
            total_pv_watts AS pv_watts,
            total_load_watts AS load_watts,
            battery_voltage,
            battery_soc,
            battery_watts,
            grid_watts,
            inverter_count,
            source
        FROM system_samples
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    conn.close()

    return dict(row) if row else None


def get_latest_inverters():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            s.timestamp,
            s.inverter_id,
            s.output_voltage,
            s.output_frequency,
            s.output_watts,
            s.output_va,
            s.load_percent,
            s.battery_voltage,
            s.battery_soc,
            s.pv_voltage,
            s.pv_current,
            s.pv_watts,
            s.status
        FROM inverter_samples AS s
        INNER JOIN (
            SELECT
                inverter_id,
                MAX(id) AS max_id
            FROM inverter_samples
            GROUP BY inverter_id
        ) AS latest
            ON s.id = latest.max_id
        ORDER BY s.inverter_id
    """).fetchall()

    conn.close()
    return [dict(row) for row in rows]


def get_recent_readings(limit=60):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            timestamp,
            total_pv_watts AS pv_watts,
            total_load_watts AS load_watts,
            battery_voltage,
            battery_soc,
            battery_watts,
            grid_watts
        FROM system_samples
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    return [dict(row) for row in reversed(rows)]


HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>EG4 Solar Monitor</title>

    <style>
        body {
            font-family: Arial, sans-serif;
            background: #111;
            color: #eee;
            margin: 0;
            padding: 20px;
        }

        h1 {
            margin-top: 0;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
        }

        .card {
            background: #1d1d1d;
            padding: 20px;
            border-radius: 10px;
        }

        .label {
            font-size: 14px;
            color: #aaa;
        }

        .value {
            font-size: 30px;
            font-weight: bold;
            margin-top: 8px;
        }

        #status {
            margin-bottom: 20px;
            color: #aaa;
        }

        .section-title {
            margin-top: 30px;
            margin-bottom: 12px;
            font-size: 22px;
        }

        .small-value {
            font-size: 22px;
            font-weight: bold;
            margin-top: 8px;
        }
    </style>
</head>

<body>

<h1>EG4 Solar Monitor</h1>

<div id="status">Loading...</div>

<div class="grid">

    <div class="card">
        <div class="label">Solar Production</div>
        <div class="value" id="pv">--</div>
    </div>

    <div class="card">
        <div class="label">House Load</div>
        <div class="value" id="load">--</div>
    </div>

    <div class="card">
        <div class="label">Battery SOC</div>
        <div class="value" id="soc">--</div>
    </div>

    <div class="card">
        <div class="label">Battery Voltage</div>
        <div class="value" id="voltage">--</div>
    </div>

    <div class="card">
        <div class="label">Battery Power</div>
        <div class="value" id="batteryPower">--</div>
    </div>

    <div class="card">
        <div class="label">Grid Power</div>
        <div class="value" id="gridPower">--</div>
    </div>

</div>

<h2 class="section-title">Inverter 1</h2>

<div class="grid">
    <div class="card">
        <div class="label">Output Power</div>
        <div class="small-value" id="inv1Output">--</div>
    </div>

    <div class="card">
        <div class="label">Load</div>
        <div class="small-value" id="inv1Load">--</div>
    </div>

    <div class="card">
        <div class="label">PV Power</div>
        <div class="small-value" id="inv1Pv">--</div>
    </div>

    <div class="card">
        <div class="label">PV Voltage</div>
        <div class="small-value" id="inv1PvVoltage">--</div>
    </div>

    <div class="card">
        <div class="label">Output Voltage</div>
        <div class="small-value" id="inv1Voltage">--</div>
    </div>

    <div class="card">
        <div class="label">Battery Voltage</div>
        <div class="small-value" id="inv1Battery">--</div>
    </div>
</div>

<h2 class="section-title">Inverter 2</h2>

<div class="grid">
    <div class="card">
        <div class="label">Output Power</div>
        <div class="small-value" id="inv2Output">--</div>
    </div>

    <div class="card">
        <div class="label">Load</div>
        <div class="small-value" id="inv2Load">--</div>
    </div>

    <div class="card">
        <div class="label">PV Power</div>
        <div class="small-value" id="inv2Pv">--</div>
    </div>

    <div class="card">
        <div class="label">PV Voltage</div>
        <div class="small-value" id="inv2PvVoltage">--</div>
    </div>

    <div class="card">
        <div class="label">Output Voltage</div>
        <div class="small-value" id="inv2Voltage">--</div>
    </div>

    <div class="card">
        <div class="label">Battery Voltage</div>
        <div class="small-value" id="inv2Battery">--</div>
    </div>
</div>

<script>

async function updateInverters() {
    try {
        const response = await fetch("/api/inverters");

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const inverters = await response.json();

        for (const inverter of inverters) {
            const prefix = "inv" + inverter.inverter_id;

            const setText = (suffix, value) => {
                const element = document.getElementById(prefix + suffix);
                if (element) {
                    element.textContent = value;
                }
            };

            setText(
                "Output",
                Number(inverter.output_watts).toFixed(0) + " W"
            );

            setText(
                "Load",
                Number(inverter.load_percent).toFixed(0) + "%"
            );

            setText(
                "Pv",
                Number(inverter.pv_watts).toFixed(0) + " W"
            );

            setText(
                "PvVoltage",
                Number(inverter.pv_voltage).toFixed(1) + " V"
            );

            setText(
                "Voltage",
                Number(inverter.output_voltage).toFixed(1) + " V"
            );

            setText(
                "Battery",
                Number(inverter.battery_voltage).toFixed(1) + " V"
            );
        }
    }
    catch (error) {
        console.error("Error loading inverter data:", error);
    }
}


async function updateDashboard() {

    try {

        const response = await fetch("/api/latest");

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const data = await response.json();

        document.getElementById("pv").textContent =
            data.pv_watts.toFixed(0) + " W";

        document.getElementById("load").textContent =
            data.load_watts.toFixed(0) + " W";

        document.getElementById("soc").textContent =
            data.battery_soc.toFixed(1) + "%";

        document.getElementById("voltage").textContent =
            data.battery_voltage.toFixed(2) + " V";

        document.getElementById("batteryPower").textContent =
            data.battery_watts.toFixed(0) + " W";

        document.getElementById("gridPower").textContent =
            data.grid_watts.toFixed(0) + " W";

        document.getElementById("status").textContent =
            "Last update: " + data.timestamp;

    }
    catch (error) {

        document.getElementById("status").textContent =
            "Error loading data: " + error;
    }
}

updateDashboard();
updateInverters();

setInterval(updateDashboard, 10000);
setInterval(updateInverters, 10000);

</script>

</body>
</html>
"""


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


@app.route("/api/history")
def api_history():
    return jsonify(get_recent_readings())


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
