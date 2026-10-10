from flask import Flask, jsonify, request, render_template_string
import json
import sqlite3
from datetime import datetime, timedelta
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


def add_gap_markers(rows, expected_seconds):
    points = []
    previous = None

    for row in rows:
        point = dict(row)

        try:
            current = datetime.fromisoformat(
                point["timestamp"]
            )
        except (TypeError, ValueError):
            current = None

        point["gap_before"] = False

        if (
            previous is not None
            and current is not None
        ):
            gap_seconds = (
                current - previous
            ).total_seconds()

            if gap_seconds > expected_seconds * 1.8:
                point["gap_before"] = True

        if current is not None:
            previous = current

        points.append(point)

    return points


def get_recent_readings(hours=24, max_points=720):
    hours = max(1, int(hours))
    max_points = max(10, int(max_points))

    conn = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )
    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA busy_timeout=10000;"
    )

    try:
        if hours <= 1:
            cutoff = (
                datetime.now()
                - timedelta(hours=hours)
            ).isoformat(timespec="seconds")

            bucket_seconds = max(
                1,
                int(
                    (hours * 3600)
                    / max_points
                ),
            )

            rows = conn.execute(
                """
                SELECT
                    MIN(timestamp) AS timestamp,
                    AVG(total_pv_watts) AS pv_value,
                    AVG(total_load_watts) AS load_value,
                    AVG(battery_soc) AS battery_soc
                FROM system_samples
                WHERE timestamp >= ?
                GROUP BY (
                    CAST(
                        strftime('%s', timestamp)
                        AS INTEGER
                    ) / ?
                )
                ORDER BY MIN(timestamp)
                """,
                (
                    cutoff,
                    bucket_seconds,
                ),
            ).fetchall()

            return {
                "mode": "power",
                "source": "raw",
                "pv_unit": "W",
                "load_unit": "W",
                "points": add_gap_markers(
                    rows,
                    max(
                        60,
                        bucket_seconds * 3,
                    ),
                ),
            }

        if hours <= 24:
            total_minutes = hours * 60

            bucket_minutes = max(
                1,
                (
                    total_minutes
                    + max_points
                    - 1
                ) // max_points,
            )

            bucket_seconds = (
                bucket_minutes * 60
            )

            rows = conn.execute(
                """
                SELECT
                    MIN(timestamp) AS timestamp,
                    AVG(pv_watts_avg) AS pv_value,
                    AVG(load_watts_avg) AS load_value,
                    AVG(battery_soc_avg) AS battery_soc
                FROM minute_summary
                WHERE datetime(timestamp) >= datetime(
                    'now',
                    'localtime',
                    ?
                )
                GROUP BY (
                    CAST(
                        strftime('%s', timestamp)
                        AS INTEGER
                    ) / ?
                )
                ORDER BY MIN(timestamp)
                """,
                (
                    f"-{hours} hours",
                    bucket_seconds,
                ),
            ).fetchall()

            return {
                "mode": "power",
                "source": "minute",
                "pv_unit": "W",
                "load_unit": "W",
                "points": add_gap_markers(
                    rows,
                    max(
                        180,
                        bucket_seconds * 3,
                    ),
                ),
            }

        if hours <= 24 * 31:
            total_quarters = hours * 4

            bucket_quarters = max(
                1,
                (
                    total_quarters
                    + max_points
                    - 1
                ) // max_points,
            )

            bucket_seconds = (
                bucket_quarters
                * 15
                * 60
            )

            rows = conn.execute(
                """
                SELECT
                    MIN(timestamp) AS timestamp,
                    AVG(
                        pv_energy_wh * 4.0
                    ) AS pv_value,
                    AVG(
                        load_energy_wh * 4.0
                    ) AS load_value,
                    AVG(
                        (
                            battery_soc_min
                            + battery_soc_max
                        ) / 2.0
                    ) AS battery_soc
                FROM quarter_hour_summary
                WHERE datetime(timestamp) >= datetime(
                    'now',
                    'localtime',
                    ?
                )
                GROUP BY (
                    CAST(
                        strftime('%s', timestamp)
                        AS INTEGER
                    ) / ?
                )
                ORDER BY MIN(timestamp)
                """,
                (
                    f"-{hours} hours",
                    bucket_seconds,
                ),
            ).fetchall()

            return {
                "mode": "power",
                "source": "quarter_hour",
                "pv_unit": "W",
                "load_unit": "W",
                "points": add_gap_markers(
                    rows,
                    max(
                        2700,
                        bucket_seconds * 3,
                    ),
                ),
            }

        days = max(
            1,
            int(round(hours / 24)),
        )

        bucket_days = max(
            1,
            (
                days
                + max_points
                - 1
            ) // max_points,
        )

        rows = conn.execute(
            """
            SELECT
                MIN(date) || 'T12:00:00'
                    AS timestamp,
                AVG(pv_energy_wh) / 1000.0
                    AS pv_value,
                AVG(load_energy_wh) / 1000.0
                    AS load_value,
                AVG(
                    (
                        battery_soc_min
                        + battery_soc_max
                    ) / 2.0
                ) AS battery_soc
            FROM daily_summary
            WHERE date >= date(
                'now',
                'localtime',
                ?
            )
            GROUP BY (
                CAST(
                    julianday(date)
                    AS INTEGER
                ) / ?
            )
            ORDER BY MIN(date)
            """,
            (
                f"-{days} days",
                bucket_days,
            ),
        ).fetchall()

        return {
            "mode": "daily_energy",
            "source": "daily",
            "pv_unit": "kWh/day",
            "load_unit": "kWh/day",
            "points": add_gap_markers(
                rows,
                bucket_days * 86400,
            ),
        }

    finally:
        conn.close()


HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <title>Solar Monitor</title>

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 20px;
            background: #111;
            color: #eee;
            font-family: Arial, sans-serif;
        }

        h1 {
            margin-top: 0;
        }

        .status {
            margin-bottom: 18px;
            color: #aaa;
        }

        .section-title {
            margin-top: 32px;
            margin-bottom: 12px;
        }

        .grid {
            display: grid;
            grid-template-columns:
                repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
        }

        .card {
            background: #1b1b1b;
            border-radius: 10px;
            padding: 16px;
        }

        .label {
            color: #aaa;
            font-size: 14px;
        }

        .value {
            margin-top: 8px;
            font-size: 28px;
            font-weight: bold;
        }

        .small-value {
            margin-top: 8px;
            font-size: 22px;
            font-weight: bold;
        }

        .quality-label {
            margin-top: 6px;
            font-size: 13px;
            opacity: 0.75;
        }

        .history-controls {
            display: flex;
            gap: 8px;
            margin: 10px 0 16px 0;
            flex-wrap: wrap;
        }

        .history-range-button {
            border: 1px solid #555;
            background: #222;
            color: #ddd;
            padding: 7px 12px;
            border-radius: 7px;
            cursor: pointer;
        }

        .history-range-button.active {
            background: #444;
            border-color: #888;
        }

        .chart-card {
            margin-top: 14px;
            padding: 16px;
            background: #1b1b1b;
            border-radius: 10px;
        }

        .chart-card canvas {
            width: 100%;
            height: 260px;
            display: block;
        }
    </style>
</head>

<body>

<h1>Solar Monitor</h1>

<div class="status" id="status">
    Waiting for telemetry...
</div>


<h2 class="section-title">System</h2>

<div class="grid">

    <div class="card">
        <div class="label">Solar Production</div>
        <div class="value" id="pvWatts">--</div>
    </div>

    <div class="card">
        <div class="label">House Load</div>
        <div class="value" id="loadWatts">--</div>
    </div>

    <div class="card">
        <div class="label">Battery SOC</div>
        <div class="value" id="batterySoc">--</div>
    </div>

    <div class="card">
        <div class="label">Battery Voltage</div>
        <div class="value" id="batteryVoltage">--</div>
    </div>

    <div class="card">
        <div class="label">Battery Power</div>
        <div class="value" id="batteryWatts">--</div>
    </div>

    <div class="card">
        <div class="label">Grid Power</div>
        <div class="value" id="gridWatts">--</div>
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
        <div class="quality-label" id="inv1PvQuality">--</div>
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
        <div class="quality-label" id="inv2PvQuality">--</div>
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


<h2 class="section-title" id="historyTitle">
    Last 24 Hours
</h2>

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


<div class="chart-card">
    <div class="label" id="pvHistoryLabel">Solar Production</div>
    <canvas id="pvChart" width="900" height="260"></canvas>
</div>

<div class="chart-card">
    <div class="label" id="loadHistoryLabel">House Load</div>
    <canvas id="loadChart" width="900" height="260"></canvas>
</div>

<div class="chart-card">
    <div class="label">Battery SOC</div>
    <canvas id="socChart" width="900" height="260"></canvas>
</div>


<script>

let historyHours = 24;


function historyLabel(hours) {
    const labels = {
        1: "Last Hour",
        6: "Last 6 Hours",
        24: "Last 24 Hours",
        168: "Last 7 Days",
        336: "Last 2 Weeks",
        720: "Last Month",
        2160: "Last 3 Months",
        4320: "Last 6 Months",
        6480: "Last 9 Months",
        8760: "Last Year",
        17520: "Last 2 Years",
        26280: "Last 3 Years",
    };

    return labels[hours] || "History";
}

async function updateDashboard() {
    try {
        const response = await fetch("/api/latest");

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const data = await response.json();

        document.getElementById("pvWatts").textContent =
            Number(data.pv_watts).toFixed(0) + " W";

        document.getElementById("loadWatts").textContent =
            Number(data.load_watts).toFixed(0) + " W";

        document.getElementById("batterySoc").textContent =
            Number(data.battery_soc).toFixed(0) + "%";

        document.getElementById("batteryVoltage").textContent =
            Number(data.battery_voltage).toFixed(1) + " V";

        document.getElementById("batteryWatts").textContent =
            Number(data.battery_watts).toFixed(0) + " W";

        document.getElementById("gridWatts").textContent =
            Number(data.grid_watts).toFixed(0) + " W";

        document.getElementById("status").textContent =
            "Last update: " +
            data.timestamp +
            " | " +
            data.inverter_count +
            " inverters | " +
            data.source;
    }
    catch (error) {
        document.getElementById("status").textContent =
            "Telemetry unavailable: " + error;
    }
}


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
                const element =
                    document.getElementById(prefix + suffix);

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

            let quality = "Unknown";

            if (
                inverter.status &&
                inverter.status.includes("pv_measured")
            ) {
                quality = "Measured";
            }
            else if (
                inverter.status &&
                inverter.status.includes("pv_estimated")
            ) {
                quality = "Estimated";
            }

            setText("PvQuality", quality);
        }
    }
    catch (error) {
        console.error(
            "Error loading inverter telemetry:",
            error
        );
    }
}


function drawLineChart(
    canvasId,
    points,
    valueKey,
    unit
) {
    const canvas =
        document.getElementById(canvasId);

    if (!canvas) {
        return;
    }

    const ctx = canvas.getContext("2d");

    const width = canvas.width;
    const height = canvas.height;

    const left = 72;
    const right = 16;
    const top = 16;
    const bottom = 38;

    const plotWidth =
        width - left - right;

    const plotHeight =
        height - top - bottom;

    ctx.clearRect(
        0,
        0,
        width,
        height
    );

    if (!points.length) {
        ctx.fillStyle = "#aaa";
        ctx.font = "14px sans-serif";

        ctx.fillText(
            "No historical data for this range.",
            left,
            top + 20
        );

        return;
    }

    const validPoints = points.filter(
        point => {
            const value =
                Number(point[valueKey]);

            const time =
                new Date(
                    point.timestamp
                ).getTime();

            return (
                Number.isFinite(value)
                && Number.isFinite(time)
            );
        }
    );

    if (!validPoints.length) {
        return;
    }

    const values = validPoints.map(
        point =>
            Number(point[valueKey])
    );

    const times = validPoints.map(
        point =>
            new Date(
                point.timestamp
            ).getTime()
    );

    let minValue = Math.min(...values);
    let maxValue = Math.max(...values);

    if (valueKey !== "battery_soc") {
        minValue = 0;
    }

    if (maxValue === minValue) {
        maxValue = minValue + 1;
    }

    const minTime = Math.min(...times);
    const maxTime = Math.max(...times);

    const timeSpan = Math.max(
        maxTime - minTime,
        1
    );

    ctx.strokeStyle = "#555";
    ctx.fillStyle = "#aaa";
    ctx.lineWidth = 1;
    ctx.font = "12px sans-serif";

    for (let i = 0; i <= 4; i++) {
        const y =
            top
            + plotHeight * i / 4;

        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(
            width - right,
            y
        );
        ctx.stroke();

        const value =
            maxValue
            - (
                (
                    maxValue
                    - minValue
                )
                * i
                / 4
            );

        const decimals =
            unit.includes("kWh")
            ? 1
            : 0;

        ctx.fillText(
            value.toFixed(decimals)
                + " "
                + unit,
            4,
            y + 4
        );
    }

    ctx.strokeStyle = "#ddd";
    ctx.lineWidth = 2;
    ctx.beginPath();

    let penDown = false;

    for (const point of validPoints) {
        const value =
            Number(point[valueKey]);

        const time =
            new Date(
                point.timestamp
            ).getTime();

        const x =
            left
            + (
                (time - minTime)
                / timeSpan
            )
            * plotWidth;

        const y =
            top
            + plotHeight
            - (
                (
                    value - minValue
                )
                / (
                    maxValue - minValue
                )
            )
            * plotHeight;

        if (
            !penDown
            || point.gap_before
        ) {
            ctx.moveTo(x, y);
            penDown = true;
        }
        else {
            ctx.lineTo(x, y);
        }
    }

    ctx.stroke();

    const first =
        new Date(
            validPoints[0].timestamp
        );

    const last =
        new Date(
            validPoints[
                validPoints.length - 1
            ].timestamp
        );

    let dateOptions;

    if (historyHours <= 24) {
        dateOptions = {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        };
    }
    else if (historyHours <= 24 * 31) {
        dateOptions = {
            month: "short",
            day: "numeric",
        };
    }
    else {
        dateOptions = {
            month: "short",
            day: "numeric",
            year: "numeric",
        };
    }

    const firstLabel =
        first.toLocaleString(
            [],
            dateOptions
        );

    const lastLabel =
        last.toLocaleString(
            [],
            dateOptions
        );

    ctx.fillStyle = "#aaa";

    ctx.fillText(
        firstLabel,
        left,
        height - 9
    );

    const endWidth =
        ctx.measureText(
            lastLabel
        ).width;

    ctx.fillText(
        lastLabel,
        width - right - endWidth,
        height - 9
    );
}

async function updateHistoryCharts() {
    try {
        const response = await fetch(
            "/api/history?hours="
            + historyHours
        );

        if (!response.ok) {
            throw new Error(
                "HTTP "
                + response.status
            );
        }

        const payload =
            await response.json();

        const history =
            payload.points || [];

        const pvUnit =
            payload.pv_unit || "W";

        const loadUnit =
            payload.load_unit || "W";

        const energyMode =
            payload.mode ===
            "daily_energy";

        document.getElementById(
            "pvHistoryLabel"
        ).textContent =
            energyMode
            ? "Solar Energy per Day"
            : "Solar Production";

        document.getElementById(
            "loadHistoryLabel"
        ).textContent =
            energyMode
            ? "House Energy per Day"
            : "House Load";

        drawLineChart(
            "pvChart",
            history,
            "pv_value",
            pvUnit
        );

        drawLineChart(
            "loadChart",
            history,
            "load_value",
            loadUnit
        );

        drawLineChart(
            "socChart",
            history,
            "battery_soc",
            "%"
        );
    }
    catch (error) {
        console.error(
            "Error loading history:",
            error
        );
    }
}

function setHistoryRange(hours) {
    historyHours = hours;

    document.getElementById(
        "historyTitle"
    ).textContent = historyLabel(hours);

    document
        .querySelectorAll(".history-range-button")
        .forEach(button => {
            button.classList.toggle(
                "active",
                Number(button.dataset.hours) === hours
            );
        });

    updateHistoryCharts();
}


document
    .querySelectorAll(".history-range-button")
    .forEach(button => {
        button.addEventListener("click", () => {
            setHistoryRange(
                Number(button.dataset.hours)
            );
        });
    });


updateDashboard();
updateInverters();
updateHistoryCharts();

setInterval(updateDashboard, 10000);
setInterval(updateInverters, 10000);
setInterval(updateHistoryCharts, 60000);

</script>

</body>
</html>
'''


@app.route("/")
def dashboard():
    return render_template_string(HTML)


@app.route("/api/latest")
def api_latest():
    reading = get_latest_reading()

    if reading is None:
        return jsonify(
            {"error": "No readings available"}
        ), 404

    return jsonify(reading)


@app.route("/api/inverters")
def api_inverters():
    return jsonify(
        get_latest_inverters()
    )


@app.route("/api/history")
def api_history():
    try:
        hours = int(
            request.args.get("hours", 24)
        )
    except (TypeError, ValueError):
        hours = 24

    hours = max(
        1,
        min(hours, 24 * 365 * 3),
    )

    return jsonify(
        get_recent_readings(
            hours=hours,
            max_points=720,
        )
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
