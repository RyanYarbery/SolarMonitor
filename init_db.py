import sqlite3

import settings

DB_PATH = settings.DB_PATH


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS inverter_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp TEXT NOT NULL,
    inverter_id INTEGER NOT NULL,

    grid_voltage REAL,
    grid_frequency REAL,

    output_voltage REAL,
    output_frequency REAL,

    output_va REAL,
    output_watts REAL,
    load_percent REAL,

    battery_voltage REAL,
    battery_charge_amps REAL,
    battery_discharge_amps REAL,
    battery_soc REAL,

    inverter_temperature_c REAL,

    pv_voltage REAL,
    pv_current REAL,
    pv_watts REAL,

    mode TEXT,
    fault_code TEXT,
    status TEXT,

    UNIQUE(timestamp, inverter_id)
);


CREATE INDEX IF NOT EXISTS idx_inverter_samples_timestamp
ON inverter_samples(timestamp);

CREATE INDEX IF NOT EXISTS idx_inverter_samples_inverter_timestamp
ON inverter_samples(inverter_id, timestamp);


CREATE TABLE IF NOT EXISTS system_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp TEXT NOT NULL UNIQUE,

    total_pv_watts REAL,
    total_load_watts REAL,

    battery_voltage REAL,
    battery_soc REAL,
    battery_watts REAL,

    grid_watts REAL,

    inverter_count INTEGER,

    source TEXT NOT NULL DEFAULT 'unknown'
);


CREATE INDEX IF NOT EXISTS idx_system_samples_timestamp
ON system_samples(timestamp);


CREATE TABLE IF NOT EXISTS battery_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp TEXT NOT NULL,
    battery_id INTEGER NOT NULL,

    voltage REAL,
    current REAL,
    soc REAL,
    soh REAL,

    temperature_c REAL,

    charge_watts REAL,
    discharge_watts REAL,

    min_cell_voltage REAL,
    max_cell_voltage REAL,
    cell_delta_mv REAL,

    cycle_count REAL,

    alarm_state TEXT,

    UNIQUE(timestamp, battery_id)
);


CREATE INDEX IF NOT EXISTS idx_battery_samples_timestamp
ON battery_samples(timestamp);


CREATE TABLE IF NOT EXISTS weather_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp TEXT NOT NULL UNIQUE,

    temperature_c REAL,
    cloud_cover_percent REAL,

    ghi_wm2 REAL,
    dni_wm2 REAL,
    dhi_wm2 REAL,

    plane_of_array_wm2 REAL,

    source TEXT
);


CREATE INDEX IF NOT EXISTS idx_weather_samples_timestamp
ON weather_samples(timestamp);


CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp TEXT NOT NULL,

    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',

    source TEXT,
    inverter_id INTEGER,
    battery_id INTEGER,

    message TEXT NOT NULL,

    cleared_at TEXT
);


CREATE INDEX IF NOT EXISTS idx_events_timestamp
ON events(timestamp);

CREATE INDEX IF NOT EXISTS idx_events_type
ON events(event_type);


CREATE TABLE IF NOT EXISTS minute_summary (
    timestamp TEXT PRIMARY KEY,

    pv_watts_avg REAL,
    pv_watts_min REAL,
    pv_watts_max REAL,

    load_watts_avg REAL,
    load_watts_min REAL,
    load_watts_max REAL,

    battery_soc_avg REAL,
    battery_soc_min REAL,
    battery_soc_max REAL,

    battery_watts_avg REAL,

    grid_watts_avg REAL,

    sample_count INTEGER NOT NULL
);


CREATE TABLE IF NOT EXISTS quarter_hour_summary (
    timestamp TEXT PRIMARY KEY,

    pv_energy_wh REAL,
    load_energy_wh REAL,

    battery_charge_wh REAL,
    battery_discharge_wh REAL,

    grid_import_wh REAL,

    pv_peak_watts REAL,
    load_peak_watts REAL,

    battery_soc_min REAL,
    battery_soc_max REAL,

    sample_count INTEGER NOT NULL
);


CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,

    pv_energy_wh REAL,
    load_energy_wh REAL,

    battery_charge_wh REAL,
    battery_discharge_wh REAL,

    grid_import_wh REAL,

    pv_peak_watts REAL,
    pv_peak_time TEXT,

    load_peak_watts REAL,
    load_peak_time TEXT,

    battery_soc_min REAL,
    battery_soc_max REAL,

    production_start TEXT,
    production_end TEXT,
    production_minutes INTEGER,

    inverter_1_pv_wh REAL,
    inverter_2_pv_wh REAL,

    equivalent_battery_cycles REAL,

    battery_round_trip_efficiency REAL,

    specific_yield_kwh_per_kw REAL,
    capacity_factor REAL,

    weather_normalized_ratio REAL,
    performance_ratio REAL
);


CREATE TABLE IF NOT EXISTS daily_inverter_summary (
    date TEXT NOT NULL,
    inverter_id INTEGER NOT NULL,

    pv_energy_wh REAL,
    load_energy_wh REAL,

    peak_pv_watts REAL,
    peak_pv_time TEXT,

    peak_load_watts REAL,
    peak_load_time TEXT,

    peak_temperature_c REAL,
    avg_temperature_c REAL,

    fault_count INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (date, inverter_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_inverter_summary_date
ON daily_inverter_summary(date);

CREATE INDEX IF NOT EXISTS idx_daily_inverter_summary_inverter
ON daily_inverter_summary(inverter_id, date);


CREATE TABLE IF NOT EXISTS monthly_summary (
    month TEXT PRIMARY KEY,

    pv_energy_wh REAL,
    load_energy_wh REAL,

    battery_charge_wh REAL,
    battery_discharge_wh REAL,

    grid_import_wh REAL,

    peak_pv_watts REAL,
    peak_load_watts REAL,

    avg_daily_pv_wh REAL,
    avg_daily_load_wh REAL,

    minimum_battery_soc REAL,

    equivalent_battery_cycles REAL,

    specific_yield_kwh_per_kw REAL,
    capacity_factor REAL,

    days_recorded INTEGER NOT NULL
);


CREATE TABLE IF NOT EXISTS yearly_summary (
    year INTEGER PRIMARY KEY,

    pv_energy_wh REAL,
    load_energy_wh REAL,

    battery_charge_wh REAL,
    battery_discharge_wh REAL,

    grid_import_wh REAL,

    peak_pv_watts REAL,
    peak_load_watts REAL,

    average_daily_pv_wh REAL,
    average_daily_load_wh REAL,

    minimum_battery_soc REAL,

    equivalent_battery_cycles REAL,

    specific_yield_kwh_per_kw REAL,
    capacity_factor REAL,

    days_recorded INTEGER NOT NULL
);
"""


SCHEMA_VERSION = "3"

# Plant facts live in the database. Operational knobs (paths, serial port,
# intervals, retention) are environment variables; see settings.py.
DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "production_threshold_watts": "100",
    "installed_pv_kw": "0",
    "battery_capacity_kwh": "0",
}


def main():
    creating_new = not DB_PATH.exists()

    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=ON;")

        conn.executescript(SCHEMA)

        for key, value in DEFAULT_CONFIG.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO system_config (key, value)
                VALUES (?, ?)
                """,
                (key, value),
            )

        conn.commit()

    finally:
        conn.close()

    if creating_new:
        print(f"Created production database: {DB_PATH}")
    else:
        print(f"Production database already exists: {DB_PATH}")
        print("If it predates schema v3, run migrate_v3.py.")

    print(f"Schema version: {SCHEMA_VERSION}")


if __name__ == "__main__":
    main()
