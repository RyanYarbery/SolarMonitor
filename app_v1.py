from flask import Flask, jsonify, render_template_string
import sqlite3

DB_PATH = "solar.db"

app = Flask(__name__)


def get_latest_reading():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = cur.execute("""
        SELECT *
        FROM readings
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    conn.close()

    return dict(row) if row else None


def get_recent_readings(limit=60):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT *
        FROM readings
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

<script>

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

setInterval(updateDashboard, 5000);

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


@app.route("/api/history")
def api_history():
    return jsonify(get_recent_readings())


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
