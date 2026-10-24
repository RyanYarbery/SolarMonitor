"""Migrate an existing database to schema v3.

Changes:
  * inverter_samples gains mode and fault_code columns
  * daily_inverter_summary is created if the v2 migration never ran
  * every stored timestamp is converted from naive local time to UTC
    ("YYYY-MM-DDTHH:MM:SS+00:00"); rows that collide after conversion
    (the repeated autumn DST hour) are dropped and will be re-derived
  * retention/interval keys leave system_config (now environment settings)

A copy of the database is written next to it as <name>.before-v3 first.
Stop the collector and aggregator before running this. Safe to re-run.
"""

import shutil
import sqlite3
import sys

import settings
from timeutil import naive_local_to_utc

# (table, [timestamp columns], has_unique_key)
TIMESTAMP_COLUMNS = [
    ("system_samples", ["timestamp"]),
    ("inverter_samples", ["timestamp"]),
    ("battery_samples", ["timestamp"]),
    ("weather_samples", ["timestamp"]),
    ("events", ["timestamp", "cleared_at"]),
    ("minute_summary", ["timestamp"]),
    ("quarter_hour_summary", ["timestamp"]),
    ("daily_summary", ["pv_peak_time", "load_peak_time", "production_start", "production_end"]),
    ("daily_inverter_summary", ["peak_pv_time", "peak_load_time"]),
]

OBSOLETE_CONFIG_KEYS = (
    "raw_retention_days",
    "minute_retention_days",
    "quarter_hour_retention_days",
    "daily_retention_days",
    "sample_interval_seconds",
)


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn, table):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def add_columns(conn):
    existing = columns(conn, "inverter_samples")
    for col in ("mode", "fault_code"):
        if col not in existing:
            conn.execute(f"ALTER TABLE inverter_samples ADD COLUMN {col} TEXT")
            print(f"Added inverter_samples.{col}")


def ensure_daily_inverter_summary(conn):
    if table_exists(conn, "daily_inverter_summary"):
        return
    import init_db

    # Pull just that table's DDL out of the canonical schema.
    start = init_db.SCHEMA.index("CREATE TABLE IF NOT EXISTS daily_inverter_summary")
    end = init_db.SCHEMA.index("CREATE TABLE IF NOT EXISTS monthly_summary")
    conn.executescript(init_db.SCHEMA[start:end])
    print("Created daily_inverter_summary")


def convert_timestamps(conn, table, cols):
    if not table_exists(conn, table):
        return
    converted = dropped = 0
    for col in cols:
        rows = conn.execute(
            f"SELECT rowid, {col} FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} NOT LIKE '%+%' AND {col} NOT LIKE '%Z'"
        ).fetchall()
        if not rows:
            continue
        with conn:
            for rowid, value in rows:
                try:
                    new_value = naive_local_to_utc(value)
                except ValueError:
                    print(f"  {table}.{col} rowid {rowid}: unparseable {value!r}, left as-is")
                    continue
                cur = conn.execute(
                    f"UPDATE OR IGNORE {table} SET {col} = ? WHERE rowid = ?",
                    (new_value, rowid),
                )
                converted += cur.rowcount
            # Anything still naive in a keyed column collided with a converted row.
            leftovers = conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} NOT LIKE '%+%' AND {col} NOT LIKE '%Z'"
            ).fetchone()[0]
            if leftovers:
                conn.execute(
                    f"DELETE FROM {table} "
                    f"WHERE {col} IS NOT NULL AND {col} NOT LIKE '%+%' AND {col} NOT LIKE '%Z'"
                )
                dropped += leftovers
    if converted or dropped:
        print(f"{table}: converted {converted} timestamps, dropped {dropped} colliding rows")


def main():
    path = settings.DB_PATH
    if not path.exists():
        print(f"{path} does not exist; nothing to migrate.")
        return 1

    backup = path.with_name(path.name + ".before-v3")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"Backup written to {backup}")

    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")

        add_columns(conn)
        ensure_daily_inverter_summary(conn)
        for table, cols in TIMESTAMP_COLUMNS:
            convert_timestamps(conn, table, cols)

        with conn:
            conn.executemany(
                "DELETE FROM system_config WHERE key = ?",
                [(k,) for k in OBSOLETE_CONFIG_KEYS],
            )
            conn.execute(
                """
                INSERT INTO system_config (key, value) VALUES ('schema_version', '3')
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()

    print("Database migrated to schema version 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
