"""Migrate an existing database to schema v3.

Changes:
  * inverter_samples gains mode, fault_code and per-string PV columns
  * daily_summary gains insolation_wh_m2
  * alert_state table for notification cooldowns
  * daily_inverter_summary is created if the v2 migration never ran
  * every stored timestamp is converted from naive local time to UTC
    ("YYYY-MM-DDTHH:MM:SS+00:00"). Raw samples recorded during the repeated
    autumn DST hour are disambiguated by insertion order (the second pass
    through 01:xx has higher ids), and every roll-up for the affected days
    is rebuilt from the repaired raw data
  * retention/interval keys leave system_config (now environment settings)

A copy of the database is written next to it as <name>.before-v3 first.
Stop the collector and aggregator before running this. Safe to re-run.
"""

import shutil
import sqlite3
import sys
from datetime import datetime

import settings
from timeutil import local_date, naive_local_to_utc, parse_iso

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


NEW_INVERTER_COLUMNS = {
    "mode": "TEXT",
    "fault_code": "TEXT",
    "pv1_voltage": "REAL",
    "pv1_current": "REAL",
    "pv1_watts": "REAL",
    "pv2_voltage": "REAL",
    "pv2_current": "REAL",
    "pv2_watts": "REAL",
}


def add_columns(conn):
    existing = columns(conn, "inverter_samples")
    for col, col_type in NEW_INVERTER_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE inverter_samples ADD COLUMN {col} {col_type}")
            print(f"Added inverter_samples.{col}")


def add_daily_columns(conn):
    existing = columns(conn, "daily_summary")
    if "insolation_wh_m2" not in existing:
        conn.execute("ALTER TABLE daily_summary ADD COLUMN insolation_wh_m2 REAL")
        print("Added daily_summary.insolation_wh_m2")


def ensure_alert_state(conn):
    if table_exists(conn, "alert_state"):
        return
    conn.execute(
        "CREATE TABLE alert_state (key TEXT PRIMARY KEY, last_sent TEXT NOT NULL, "
        "active INTEGER NOT NULL DEFAULT 0)"
    )
    print("Created alert_state")


def ensure_daily_inverter_summary(conn):
    if table_exists(conn, "daily_inverter_summary"):
        return
    import init_db

    # Pull just that table's DDL out of the canonical schema.
    start = init_db.SCHEMA.index("CREATE TABLE IF NOT EXISTS daily_inverter_summary")
    end = init_db.SCHEMA.index("CREATE TABLE IF NOT EXISTS monthly_summary")
    conn.executescript(init_db.SCHEMA[start:end])
    print("Created daily_inverter_summary")


def is_ambiguous(value):
    """True when a naive local time occurs twice (autumn DST fall-back)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        return False
    return (
        dt.replace(fold=0).astimezone().utcoffset() != dt.replace(fold=1).astimezone().utcoffset()
    )


def assign_folds(rows):
    """Given (rowid, naive_ts) in insertion order, return {rowid: fold}.

    During the fall-back hour the collector writes 01:00..01:59 twice. The
    second pass is detectable because the timestamp jumps backwards while
    the value is ambiguous; everything from there until the ambiguity ends
    belongs to the later (standard-time) hour, fold=1.
    """
    folds = {}
    in_second_pass = False
    prev = None
    for rowid, ts in rows:
        ambiguous = is_ambiguous(ts)
        if ambiguous and prev is not None and ts < prev:
            in_second_pass = True
        if not ambiguous:
            in_second_pass = False
        folds[rowid] = 1 if (ambiguous and in_second_pass) else 0
        prev = ts
    return folds


def convert_timestamps(conn, table, cols):
    """Returns the set of local dates whose rows had ambiguous timestamps."""
    if not table_exists(conn, table):
        return set()
    converted = dropped = 0
    affected_days = set()
    has_id = "id" in columns(conn, table)
    for col in cols:
        order = "id" if has_id else "rowid"
        rows = conn.execute(
            f"SELECT rowid, {col} FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} NOT LIKE '%+%' AND {col} NOT LIKE '%Z' "
            f"ORDER BY {order}"
        ).fetchall()
        if not rows:
            continue
        folds = assign_folds(rows) if has_id else {}
        with conn:
            for rowid, value in rows:
                try:
                    new_value = naive_local_to_utc(value, fold=folds.get(rowid, 0))
                    if is_ambiguous(value):
                        affected_days.add(local_date(parse_iso(new_value)))
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
    return affected_days


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
        add_daily_columns(conn)
        ensure_daily_inverter_summary(conn)
        ensure_alert_state(conn)
        affected_days = set()
        for table, cols in TIMESTAMP_COLUMNS:
            if table in ("system_samples", "inverter_samples"):
                affected_days |= convert_timestamps(conn, table, cols)
            else:
                convert_timestamps(conn, table, cols)

        import init_db

        with conn:
            for key, value in init_db.DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)", (key, value)
                )
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
        if affected_days:
            import aggregator

            conn.row_factory = sqlite3.Row
            days = sorted(affected_days)
            print(
                f"Rebuilding roll-ups for DST-affected day(s): {', '.join(d.isoformat() for d in days)}"
            )
            aggregator.rebuild_days(conn, days)

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()

    print("Database migrated to schema version 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
