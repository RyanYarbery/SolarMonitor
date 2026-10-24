"""Incremental roll-ups of raw telemetry into minute, 15-minute, daily,
monthly and yearly summaries, followed by guarded retention.

Only a small overlap near the newest data is recomputed on each run so the
job stays cheap on a Raspberry Pi 1. Minute and quarter-hour buckets are
keyed in UTC; daily and longer summaries are keyed on local calendar dates.
"""

import sqlite3
from collections import defaultdict
from datetime import date, timedelta

import alerts
import settings
from timeutil import (
    floor_minute,
    floor_quarter_hour,
    local_date,
    local_day_bounds,
    now_utc,
    parse_iso,
    to_iso,
    today_local,
)

# Recalculate a small overlap so the newest buckets can be corrected
# after late/new samples arrive.
MINUTE_OVERLAP = timedelta(minutes=2)
QUARTER_OVERLAP = timedelta(minutes=30)
DAILY_OVERLAP_DAYS = 1

# Do not integrate energy across telemetry outages longer than this.
MAX_INTEGRATION_GAP_SECONDS = 60

NO_FAULT = (None, "", "00", "0")


def connect_db():
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


# -------------------------------------------------------------------- helpers


def _clean(values):
    return [v for v in values if v is not None]


def average(values):
    values = _clean(values)
    return sum(values) / len(values) if values else None


def minimum(values):
    values = _clean(values)
    return min(values) if values else None


def maximum(values):
    values = _clean(values)
    return max(values) if values else None


def integrate_energy(samples, key, transform=None):
    """Trapezoidal integral of samples[key] (watts) over time -> watt-hours.

    `transform` maps a raw value before integration (e.g. clamp to charge
    only). Pairs separated by more than MAX_INTEGRATION_GAP_SECONDS are
    skipped so outages do not get back-filled.
    """
    valid = [s for s in samples if s[key] is not None]
    if len(valid) < 2:
        return 0.0

    f = transform or (lambda v: v)
    total_wh = 0.0
    prev_t = parse_iso(valid[0]["timestamp"])
    prev_v = f(float(valid[0][key]))

    for current in valid[1:]:
        t = parse_iso(current["timestamp"])
        v = f(float(current[key]))
        seconds = (t - prev_t).total_seconds()
        if 0 < seconds <= MAX_INTEGRATION_GAP_SECONDS:
            total_wh += (prev_v + v) / 2.0 * seconds / 3600.0
        prev_t, prev_v = t, v

    return total_wh


def charge_only(v):
    return max(v, 0.0)


def discharge_only(v):
    return max(-v, 0.0)


def peak(samples, key):
    valid = [s for s in samples if s[key] is not None]
    return max(valid, key=lambda s: s[key]) if valid else None


def get_latest_timestamp(conn, table):
    row = conn.execute(f"SELECT MAX(timestamp) FROM {table}").fetchone()
    return parse_iso(row[0]) if row and row[0] else None


def get_raw_start(conn, table="system_samples"):
    row = conn.execute(f"SELECT MIN(timestamp) FROM {table}").fetchone()
    return parse_iso(row[0]) if row and row[0] else None


def load_system_rows(conn, start, end=None):
    sql = """
        SELECT timestamp, total_pv_watts, total_load_watts,
               battery_soc, battery_watts, grid_watts
        FROM system_samples
        WHERE timestamp >= ?
    """
    args = [to_iso(start)]
    if end is not None:
        sql += " AND timestamp < ?"
        args.append(to_iso(end))
    sql += " ORDER BY timestamp"
    return conn.execute(sql, args).fetchall()


def load_config(conn):
    return dict(conn.execute("SELECT key, value FROM system_config").fetchall())


def upsert(conn, table, key_cols, values):
    """INSERT ... ON CONFLICT(key) DO UPDATE for a dict of column -> value."""
    cols = list(values)
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key_cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key_cols)}) DO UPDATE SET {updates}",
        [values[c] for c in cols],
    )


def _bucketed(conn, summary_table, overlap, floor_fn):
    """Yield (bucket_iso, rows) for every complete bucket needing (re)compute."""
    raw_start = get_raw_start(conn)
    if raw_start is None:
        return {}
    latest = get_latest_timestamp(conn, summary_table)
    start = raw_start if latest is None else max(raw_start, latest - overlap)
    end = floor_fn(now_utc())  # never summarise the current, incomplete bucket

    groups = defaultdict(list)
    for row in load_system_rows(conn, start, end):
        bucket = floor_fn(parse_iso(row["timestamp"]))
        groups[to_iso(bucket)].append(row)
    return groups


# ------------------------------------------------------------------- roll-ups


def aggregate_minutes(conn):
    count = 0
    for ts, samples in _bucketed(conn, "minute_summary", MINUTE_OVERLAP, floor_minute).items():
        pv = [r["total_pv_watts"] for r in samples]
        load = [r["total_load_watts"] for r in samples]
        soc = [r["battery_soc"] for r in samples]
        upsert(
            conn,
            "minute_summary",
            ["timestamp"],
            {
                "timestamp": ts,
                "pv_watts_avg": average(pv),
                "pv_watts_min": minimum(pv),
                "pv_watts_max": maximum(pv),
                "load_watts_avg": average(load),
                "load_watts_min": minimum(load),
                "load_watts_max": maximum(load),
                "battery_soc_avg": average(soc),
                "battery_soc_min": minimum(soc),
                "battery_soc_max": maximum(soc),
                "battery_watts_avg": average([r["battery_watts"] for r in samples]),
                "grid_watts_avg": average([r["grid_watts"] for r in samples]),
                "sample_count": len(samples),
            },
        )
        count += 1
    conn.commit()
    return count


def aggregate_quarter_hours(conn):
    count = 0
    for ts, samples in _bucketed(
        conn, "quarter_hour_summary", QUARTER_OVERLAP, floor_quarter_hour
    ).items():
        soc = [r["battery_soc"] for r in samples]
        upsert(
            conn,
            "quarter_hour_summary",
            ["timestamp"],
            {
                "timestamp": ts,
                "pv_energy_wh": integrate_energy(samples, "total_pv_watts"),
                "load_energy_wh": integrate_energy(samples, "total_load_watts"),
                "battery_charge_wh": integrate_energy(samples, "battery_watts", charge_only),
                "battery_discharge_wh": integrate_energy(samples, "battery_watts", discharge_only),
                "grid_import_wh": integrate_energy(samples, "grid_watts", charge_only),
                "pv_peak_watts": maximum([r["total_pv_watts"] for r in samples]),
                "load_peak_watts": maximum([r["total_load_watts"] for r in samples]),
                "battery_soc_min": minimum(soc),
                "battery_soc_max": maximum(soc),
                "sample_count": len(samples),
            },
        )
        count += 1
    conn.commit()
    return count


def _daily_date_range(conn, raw_table, summary_table):
    """Local dates to (re)compute: from the newest summary minus overlap
    (or the oldest raw sample) up to, but excluding, today."""
    raw_start = get_raw_start(conn, raw_table)
    if raw_start is None:
        return None, None
    first_raw_day = local_date(raw_start)

    row = conn.execute(f"SELECT MAX(date) FROM {summary_table}").fetchone()
    if row and row[0]:
        latest = date.fromisoformat(row[0])
        start = max(first_raw_day, latest - timedelta(days=DAILY_OVERLAP_DAYS))
    else:
        start = first_raw_day
    return start, today_local()


def daily_insolation_wh_m2(conn, day_start, day_end):
    """Sum of hourly GHI rows (W/m² mean over the hour == Wh/m²) for the day.

    Returns None unless the day is essentially fully covered, so a partial
    weather fetch does not produce a wildly wrong performance ratio.
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(ghi_wm2) FROM weather_samples "
        "WHERE timestamp > ? AND timestamp <= ? AND ghi_wm2 IS NOT NULL",
        (to_iso(day_start), to_iso(day_end)),
    ).fetchone()
    hours = (day_end - day_start).total_seconds() / 3600
    if row is None or row[0] < hours - 1:
        return None
    return float(row[1] or 0.0)


def aggregate_daily(conn):
    start, today = _daily_date_range(conn, "system_samples", "daily_summary")
    if start is None:
        return 0

    config = load_config(conn)
    production_threshold = float(config.get("production_threshold_watts", 100))
    installed_pv_kw = float(config.get("installed_pv_kw", 0))
    battery_capacity_kwh = float(config.get("battery_capacity_kwh", 0))

    count = 0
    day = start
    while day < today:  # only completed days
        day_start, day_end = local_day_bounds(day)
        samples = load_system_rows(conn, day_start, day_end)
        day += timedelta(days=1)
        if not samples:
            continue

        pv_wh = integrate_energy(samples, "total_pv_watts")
        load_wh = integrate_energy(samples, "total_load_watts")
        charge_wh = integrate_energy(samples, "battery_watts", charge_only)
        discharge_wh = integrate_energy(samples, "battery_watts", discharge_only)
        grid_wh = integrate_energy(samples, "grid_watts", charge_only)

        pv_peak = peak(samples, "total_pv_watts")
        load_peak = peak(samples, "total_load_watts")
        soc = [r["battery_soc"] for r in samples]

        insolation = daily_insolation_wh_m2(conn, day_start, day_end)
        performance_ratio = None
        if insolation and insolation > 0 and installed_pv_kw > 0:
            # PR = E_actual[kWh] / (P_nameplate[kWp] * H[kWh/m²])
            performance_ratio = pv_wh / (installed_pv_kw * insolation)

        producing = [
            r
            for r in samples
            if r["total_pv_watts"] is not None and r["total_pv_watts"] >= production_threshold
        ]
        production_start = production_end = None
        production_minutes = 0
        if producing:
            production_start = producing[0]["timestamp"]
            production_end = producing[-1]["timestamp"]
            duration = parse_iso(production_end) - parse_iso(production_start)
            production_minutes = max(0, int(duration.total_seconds() / 60))

        upsert(
            conn,
            "daily_summary",
            ["date"],
            {
                "date": (day - timedelta(days=1)).isoformat(),
                "pv_energy_wh": pv_wh,
                "load_energy_wh": load_wh,
                "battery_charge_wh": charge_wh,
                "battery_discharge_wh": discharge_wh,
                "grid_import_wh": grid_wh,
                "pv_peak_watts": pv_peak["total_pv_watts"] if pv_peak else None,
                "pv_peak_time": pv_peak["timestamp"] if pv_peak else None,
                "load_peak_watts": load_peak["total_load_watts"] if load_peak else None,
                "load_peak_time": load_peak["timestamp"] if load_peak else None,
                "battery_soc_min": minimum(soc),
                "battery_soc_max": maximum(soc),
                "production_start": production_start,
                "production_end": production_end,
                "production_minutes": production_minutes,
                "equivalent_battery_cycles": (
                    (charge_wh + discharge_wh) / 1000.0 / (2 * battery_capacity_kwh)
                    if battery_capacity_kwh > 0
                    else None
                ),
                "battery_round_trip_efficiency": (
                    discharge_wh / charge_wh if charge_wh > 0 else None
                ),
                "specific_yield_kwh_per_kw": (
                    pv_wh / 1000.0 / installed_pv_kw if installed_pv_kw > 0 else None
                ),
                "capacity_factor": (
                    pv_wh / 1000.0 / (installed_pv_kw * 24) if installed_pv_kw > 0 else None
                ),
                "insolation_wh_m2": insolation,
                "performance_ratio": performance_ratio,
            },
        )
        conn.commit()  # keep the writer transaction short
        count += 1
    return count


def aggregate_daily_inverters(conn):
    start, today = _daily_date_range(conn, "inverter_samples", "daily_inverter_summary")
    if start is None:
        return 0

    count = 0
    day = start
    while day < today:
        day_start, day_end = local_day_bounds(day)
        rows = conn.execute(
            """
            SELECT timestamp, inverter_id, pv_watts, output_watts,
                   inverter_temperature_c, fault_code
            FROM inverter_samples
            WHERE timestamp >= ? AND timestamp < ?
            ORDER BY inverter_id, timestamp
            """,
            (to_iso(day_start), to_iso(day_end)),
        ).fetchall()

        groups = defaultdict(list)
        for row in rows:
            groups[row["inverter_id"]].append(row)

        for inverter_id, samples in groups.items():
            pv_peak = peak(samples, "pv_watts")
            load_peak = peak(samples, "output_watts")
            temps = [r["inverter_temperature_c"] for r in samples]
            upsert(
                conn,
                "daily_inverter_summary",
                ["date", "inverter_id"],
                {
                    "date": day.isoformat(),
                    "inverter_id": inverter_id,
                    "pv_energy_wh": integrate_energy(samples, "pv_watts"),
                    "load_energy_wh": integrate_energy(samples, "output_watts"),
                    "peak_pv_watts": pv_peak["pv_watts"] if pv_peak else None,
                    "peak_pv_time": pv_peak["timestamp"] if pv_peak else None,
                    "peak_load_watts": load_peak["output_watts"] if load_peak else None,
                    "peak_load_time": load_peak["timestamp"] if load_peak else None,
                    "peak_temperature_c": maximum(temps),
                    "avg_temperature_c": average(temps),
                    "fault_count": sum(1 for r in samples if r["fault_code"] not in NO_FAULT),
                },
            )
            if inverter_id in (1, 2):
                conn.execute(
                    f"UPDATE daily_summary SET inverter_{inverter_id}_pv_wh = ? WHERE date = ?",
                    (integrate_energy(samples, "pv_watts"), day.isoformat()),
                )
            count += 1
        conn.commit()
        day += timedelta(days=1)
    return count


def _aggregate_periods(conn, table, key_col, key_fn, avg_prefix, since=None):
    """Rebuild period rows. With `since` (a local date) only periods containing
    that date or later are recomputed; a still-empty table is built in full."""
    if since is not None and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
        period_start = since.replace(day=1) if key_col == "month" else since.replace(month=1, day=1)
        rows = conn.execute(
            "SELECT * FROM daily_summary WHERE date >= ? ORDER BY date",
            (period_start.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM daily_summary ORDER BY date").fetchall()
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row["date"])].append(row)

    count = 0
    for key, days in groups.items():
        n = len(days)
        pv = sum(r["pv_energy_wh"] or 0.0 for r in days)
        load = sum(r["load_energy_wh"] or 0.0 for r in days)
        upsert(
            conn,
            table,
            [key_col],
            {
                key_col: key,
                "pv_energy_wh": pv,
                "load_energy_wh": load,
                "battery_charge_wh": sum(r["battery_charge_wh"] or 0.0 for r in days),
                "battery_discharge_wh": sum(r["battery_discharge_wh"] or 0.0 for r in days),
                "grid_import_wh": sum(r["grid_import_wh"] or 0.0 for r in days),
                "peak_pv_watts": maximum([r["pv_peak_watts"] for r in days]),
                "peak_load_watts": maximum([r["load_peak_watts"] for r in days]),
                f"{avg_prefix}_daily_pv_wh": pv / n,
                f"{avg_prefix}_daily_load_wh": load / n,
                "minimum_battery_soc": minimum([r["battery_soc_min"] for r in days]),
                "equivalent_battery_cycles": sum(
                    r["equivalent_battery_cycles"] or 0.0 for r in days
                ),
                "days_recorded": n,
            },
        )
        count += 1
    conn.commit()
    return count


def aggregate_monthly(conn, since=None):
    return _aggregate_periods(conn, "monthly_summary", "month", lambda d: d[:7], "avg", since)


def aggregate_yearly(conn, since=None):
    return _aggregate_periods(
        conn, "yearly_summary", "year", lambda d: int(d[:4]), "average", since
    )


# ------------------------------------------------------------------ retention


def _days_with_samples(conn, table, first_day, last_day, inverter_id=None):
    """Local dates in [first_day, last_day) that have at least one raw row."""
    days = set()
    day = first_day
    while day < last_day:
        start, end = local_day_bounds(day)
        sql = f"SELECT 1 FROM {table} WHERE timestamp >= ? AND timestamp < ?"
        args = [to_iso(start), to_iso(end)]
        if inverter_id is not None:
            sql += " AND inverter_id = ?"
            args.append(inverter_id)
        if conn.execute(sql + " LIMIT 1", args).fetchone():
            days.add(day.isoformat())
        day += timedelta(days=1)
    return days


def prune_old_data(conn, raw_days=None, minute_days=None):
    """Delete raw samples older than the retention window, but only once
    every affected local day has a daily summary (system and per-inverter).

    The cutoff is a local midnight so a day is either wholly kept or wholly
    eligible; the previous implementation compared a mid-day cutoff against
    whole-day summaries and always found the cutoff day "missing".
    """
    raw_days = settings.RAW_RETENTION_DAYS if raw_days is None else raw_days
    minute_days = settings.MINUTE_RETENTION_DAYS if minute_days is None else minute_days

    result = {"system_samples": 0, "inverter_samples": 0, "minute_summary": 0}

    if raw_days > 0:
        cutoff_day = today_local() - timedelta(days=raw_days)
        cutoff_utc, _ = local_day_bounds(cutoff_day)
        cutoff = to_iso(cutoff_utc)

        raw_start = get_raw_start(conn, "system_samples")
        if raw_start is not None:
            first_day = local_date(raw_start)
            needed = _days_with_samples(conn, "system_samples", first_day, cutoff_day)
            have = {
                r[0]
                for r in conn.execute(
                    "SELECT date FROM daily_summary WHERE date < ?", (cutoff_day.isoformat(),)
                )
            }
            missing = needed - have
            if missing:
                raise RuntimeError(
                    "Retention aborted: missing system daily summaries for "
                    + ", ".join(sorted(missing))
                )

        inv_start = get_raw_start(conn, "inverter_samples")
        if inv_start is not None:
            first_day = local_date(inv_start)
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT inverter_id FROM inverter_samples ORDER BY inverter_id"
                )
            ]
            for inverter_id in ids:
                needed = _days_with_samples(
                    conn, "inverter_samples", first_day, cutoff_day, inverter_id
                )
                have = {
                    r[0]
                    for r in conn.execute(
                        "SELECT date FROM daily_inverter_summary "
                        "WHERE inverter_id = ? AND date < ?",
                        (inverter_id, cutoff_day.isoformat()),
                    )
                }
                missing = needed - have
                if missing:
                    raise RuntimeError(
                        f"Retention aborted: inverter {inverter_id} is missing daily "
                        "summaries for " + ", ".join(sorted(missing))
                    )

        with conn:
            result["system_samples"] = conn.execute(
                "DELETE FROM system_samples WHERE timestamp < ?", (cutoff,)
            ).rowcount
            result["inverter_samples"] = conn.execute(
                "DELETE FROM inverter_samples WHERE timestamp < ?", (cutoff,)
            ).rowcount

    if minute_days > 0:
        minute_cutoff = to_iso(now_utc() - timedelta(days=minute_days))
        with conn:
            result["minute_summary"] = conn.execute(
                "DELETE FROM minute_summary WHERE timestamp < ?", (minute_cutoff,)
            ).rowcount

    return result


# ----------------------------------------------------------------------- main


def check_telemetry_age(conn, stale_after=None):
    """Alert (with cooldown) when the collector has stopped writing.

    The collector cannot report its own death; this runs from the timer.
    Returns the age in seconds, or None when there is no data at all.
    """
    stale_after = settings.STALE_ALERT_AFTER if stale_after is None else stale_after
    latest = get_latest_timestamp(conn, "system_samples")
    if latest is None:
        return None
    age = (now_utc() - latest).total_seconds()
    with conn:
        if age > stale_after:
            alerts.raise_alert(
                conn,
                "telemetry_stale",
                "Solar telemetry stale",
                f"Last sample {int(age // 60)} minutes ago. Collector may be down.",
                "warning",
                ("warning",),
            )
        else:
            alerts.clear_alert(
                conn,
                "telemetry_stale",
                "Solar telemetry resumed",
                f"Latest sample is {int(age)} seconds old.",
            )
    return age


def run(conn):
    daily_since, _ = _daily_date_range(conn, "system_samples", "daily_summary")
    print(f"Minute buckets updated: {aggregate_minutes(conn)}")
    print(f"15-minute buckets updated: {aggregate_quarter_hours(conn)}")
    print(f"Daily summaries updated: {aggregate_daily(conn)}")
    print(f"Daily inverter summaries updated: {aggregate_daily_inverters(conn)}")
    print(f"Monthly summaries updated: {aggregate_monthly(conn, daily_since)}")
    print(f"Yearly summaries updated: {aggregate_yearly(conn, daily_since)}")
    age = check_telemetry_age(conn)
    print(f"Newest sample age: {'n/a' if age is None else f'{int(age)} s'}")
    deleted = prune_old_data(conn)
    print(
        f"Retention pruned: {deleted['system_samples']} system, "
        f"{deleted['inverter_samples']} inverter, {deleted['minute_summary']} minute"
    )


def main():
    if not settings.DB_PATH.exists():
        raise FileNotFoundError(f"{settings.DB_PATH} does not exist")
    print("Running incremental solar aggregation...")
    conn = connect_db()
    try:
        run(conn)
    finally:
        conn.close()
    print("Incremental aggregation complete.")


if __name__ == "__main__":
    main()
