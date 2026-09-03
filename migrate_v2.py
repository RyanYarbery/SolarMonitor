import sqlite3

DB_PATH = "solar_v1.db"

SCHEMA = """
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
"""


def main():
    conn = sqlite3.connect(DB_PATH)

    try:
        conn.executescript(SCHEMA)

        conn.execute("""
            INSERT INTO system_config (key, value)
            VALUES ('schema_version', '2')
            ON CONFLICT(key)
            DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
        """)

        conn.commit()

    finally:
        conn.close()

    print("Database migrated to schema version 2.")


if __name__ == "__main__":
    main()
