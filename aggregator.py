import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("solar_v1.db")

# Recalculate a small overlap so the newest buckets can be corrected
# after late/new samples arrive.
MINUTE_OVERLAP = timedelta(minutes=2)
QUARTER_OVERLAP = timedelta(minutes=30)
DAILY_OVERLAP = timedelta(days=1)


def connect_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")

    return conn


def parse_timestamp(value):
    return datetime.fromisoformat(value)


def floor_minute(dt):
    return dt.replace(
        second=0,
        microsecond=0,
    )


def floor_quarter_hour(dt):
    return dt.replace(
        minute=(dt.minute // 15) * 15,
        second=0,
        microsecond=0,
    )


def average(values):
    values = [
        value
        for value in values
        if value is not None
    ]

    if not values:
        return None

    return sum(values) / len(values)


def minimum(values):
    values = [
        value
        for value in values
        if value is not None
    ]

    return min(values) if values else None


def maximum(values):
    values = [
        value
        for value in values
        if value is not None
    ]

    return max(values) if values else None


def integrate_energy(samples, key):
    valid = [
        sample
        for sample in samples
        if sample[key] is not None
    ]

    if len(valid) < 2:
        return 0.0

    total_wh = 0.0

    for previous, current in zip(
        valid,
        valid[1:],
    ):
        t1 = parse_timestamp(
            previous["timestamp"]
        )

        t2 = parse_timestamp(
            current["timestamp"]
        )

        seconds = (
            t2 - t1
        ).total_seconds()

        if seconds <= 0:
            continue

        # Do not integrate across telemetry outages.
        if seconds > 60:
            continue

        p1 = float(previous[key])
        p2 = float(current[key])

        total_wh += (
            (p1 + p2)
            / 2.0
            * seconds
            / 3600.0
        )

    return total_wh


def get_latest_timestamp(conn, table):
    row = conn.execute(
        f"""
        SELECT MAX(timestamp)
        FROM {table}
        """
    ).fetchone()

    if not row or row[0] is None:
        return None

    return parse_timestamp(row[0])


def get_latest_date(conn, table):
    row = conn.execute(
        f"""
        SELECT MAX(date)
        FROM {table}
        """
    ).fetchone()

    if not row or row[0] is None:
        return None

    return datetime.fromisoformat(row[0])


def get_raw_start(conn):
    row = conn.execute(
        """
        SELECT MIN(timestamp)
        FROM system_samples
        """
    ).fetchone()

    if not row or row[0] is None:
        return None

    return parse_timestamp(row[0])


def load_system_rows(
    conn,
    start,
    end=None,
):
    sql = """
        SELECT
            timestamp,
            total_pv_watts,
            total_load_watts,
            battery_soc,
            battery_watts,
            grid_watts
        FROM system_samples
        WHERE timestamp >= ?
    """

    args = [
        start.isoformat(
            timespec="seconds"
        )
    ]

    if end is not None:
        sql += """
            AND timestamp < ?
        """

        args.append(
            end.isoformat(
                timespec="seconds"
            )
        )

    sql += """
        ORDER BY timestamp
    """

    return conn.execute(
        sql,
        args,
    ).fetchall()


def aggregate_minutes(conn):
    raw_start = get_raw_start(conn)

    if raw_start is None:
        return 0

    latest = get_latest_timestamp(
        conn,
        "minute_summary",
    )

    if latest is None:
        start = raw_start
    else:
        start = max(
            raw_start,
            latest - MINUTE_OVERLAP,
        )

    now = datetime.now()

    # Never summarize the currently incomplete minute.
    end = floor_minute(now)

    rows = load_system_rows(
        conn,
        start,
        end,
    )

    groups = defaultdict(list)

    for row in rows:
        bucket = floor_minute(
            parse_timestamp(
                row["timestamp"]
            )
        )

        groups[
            bucket.isoformat(
                timespec="seconds"
            )
        ].append(row)

    count = 0

    for timestamp, samples in groups.items():
        pv = [
            r["total_pv_watts"]
            for r in samples
        ]

        load = [
            r["total_load_watts"]
            for r in samples
        ]

        soc = [
            r["battery_soc"]
            for r in samples
        ]

        battery = [
            r["battery_watts"]
            for r in samples
        ]

        grid = [
            r["grid_watts"]
            for r in samples
        ]

        conn.execute(
            """
            INSERT INTO minute_summary (
                timestamp,
                pv_watts_avg,
                pv_watts_min,
                pv_watts_max,
                load_watts_avg,
                load_watts_min,
                load_watts_max,
                battery_soc_avg,
                battery_soc_min,
                battery_soc_max,
                battery_watts_avg,
                grid_watts_avg,
                sample_count
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(timestamp)
            DO UPDATE SET
                pv_watts_avg =
                    excluded.pv_watts_avg,
                pv_watts_min =
                    excluded.pv_watts_min,
                pv_watts_max =
                    excluded.pv_watts_max,
                load_watts_avg =
                    excluded.load_watts_avg,
                load_watts_min =
                    excluded.load_watts_min,
                load_watts_max =
                    excluded.load_watts_max,
                battery_soc_avg =
                    excluded.battery_soc_avg,
                battery_soc_min =
                    excluded.battery_soc_min,
                battery_soc_max =
                    excluded.battery_soc_max,
                battery_watts_avg =
                    excluded.battery_watts_avg,
                grid_watts_avg =
                    excluded.grid_watts_avg,
                sample_count =
                    excluded.sample_count
            """,
            (
                timestamp,
                average(pv),
                minimum(pv),
                maximum(pv),
                average(load),
                minimum(load),
                maximum(load),
                average(soc),
                minimum(soc),
                maximum(soc),
                average(battery),
                average(grid),
                len(samples),
            ),
        )

        count += 1

    conn.commit()

    return count


def aggregate_quarter_hours(conn):
    raw_start = get_raw_start(conn)

    if raw_start is None:
        return 0

    latest = get_latest_timestamp(
        conn,
        "quarter_hour_summary",
    )

    if latest is None:
        start = raw_start
    else:
        start = max(
            raw_start,
            latest - QUARTER_OVERLAP,
        )

    now = datetime.now()

    # Never summarize an incomplete 15-minute bucket.
    end = floor_quarter_hour(now)

    rows = load_system_rows(
        conn,
        start,
        end,
    )

    groups = defaultdict(list)

    for row in rows:
        bucket = floor_quarter_hour(
            parse_timestamp(
                row["timestamp"]
            )
        )

        groups[
            bucket.isoformat(
                timespec="seconds"
            )
        ].append(row)

    count = 0

    for timestamp, samples in groups.items():

        pv_energy_wh = integrate_energy(
            samples,
            "total_pv_watts",
        )

        load_energy_wh = integrate_energy(
            samples,
            "total_load_watts",
        )

        charge_samples = []
        discharge_samples = []
        grid_samples = []

        for row in samples:
            row_dict = dict(row)

            battery_watts = (
                row["battery_watts"]
                or 0.0
            )

            grid_watts = (
                row["grid_watts"]
                or 0.0
            )

            charge = dict(row_dict)
            charge["energy_value"] = max(
                battery_watts,
                0.0,
            )

            discharge = dict(row_dict)
            discharge["energy_value"] = max(
                -battery_watts,
                0.0,
            )

            grid = dict(row_dict)
            grid["energy_value"] = max(
                grid_watts,
                0.0,
            )

            charge_samples.append(charge)
            discharge_samples.append(
                discharge
            )
            grid_samples.append(grid)

        pv_values = [
            r["total_pv_watts"]
            for r in samples
            if r["total_pv_watts"]
            is not None
        ]

        load_values = [
            r["total_load_watts"]
            for r in samples
            if r["total_load_watts"]
            is not None
        ]

        soc_values = [
            r["battery_soc"]
            for r in samples
            if r["battery_soc"]
            is not None
        ]

        conn.execute(
            """
            INSERT INTO quarter_hour_summary (
                timestamp,
                pv_energy_wh,
                load_energy_wh,
                battery_charge_wh,
                battery_discharge_wh,
                grid_import_wh,
                pv_peak_watts,
                load_peak_watts,
                battery_soc_min,
                battery_soc_max,
                sample_count
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )

            ON CONFLICT(timestamp)
            DO UPDATE SET
                pv_energy_wh =
                    excluded.pv_energy_wh,
                load_energy_wh =
                    excluded.load_energy_wh,
                battery_charge_wh =
                    excluded.battery_charge_wh,
                battery_discharge_wh =
                    excluded.battery_discharge_wh,
                grid_import_wh =
                    excluded.grid_import_wh,
                pv_peak_watts =
                    excluded.pv_peak_watts,
                load_peak_watts =
                    excluded.load_peak_watts,
                battery_soc_min =
                    excluded.battery_soc_min,
                battery_soc_max =
                    excluded.battery_soc_max,
                sample_count =
                    excluded.sample_count
            """,
            (
                timestamp,
                pv_energy_wh,
                load_energy_wh,
                integrate_energy(
                    charge_samples,
                    "energy_value",
                ),
                integrate_energy(
                    discharge_samples,
                    "energy_value",
                ),
                integrate_energy(
                    grid_samples,
                    "energy_value",
                ),
                (
                    max(pv_values)
                    if pv_values
                    else None
                ),
                (
                    max(load_values)
                    if load_values
                    else None
                ),
                (
                    min(soc_values)
                    if soc_values
                    else None
                ),
                (
                    max(soc_values)
                    if soc_values
                    else None
                ),
                len(samples),
            ),
        )

        count += 1

    conn.commit()

    return count


def aggregate_daily(conn):
    raw_start = get_raw_start(conn)

    if raw_start is None:
        return 0

    latest = get_latest_date(
        conn,
        "daily_summary",
    )

    if latest is None:
        start_date = raw_start.date()
    else:
        start_date = (
            latest - DAILY_OVERLAP
        ).date()

        if start_date < raw_start.date():
            start_date = raw_start.date()

    today = datetime.now().date()

    config = dict(
        conn.execute(
            """
            SELECT key, value
            FROM system_config
            """
        ).fetchall()
    )

    production_threshold = float(
        config.get(
            "production_threshold_watts",
            100,
        )
    )

    installed_pv_kw = float(
        config.get(
            "installed_pv_kw",
            0,
        )
    )

    battery_capacity_kwh = float(
        config.get(
            "battery_capacity_kwh",
            0,
        )
    )

    count = 0
    current_date = start_date

    # Only write completed days.
    while current_date < today:

        day_start = datetime.combine(
            current_date,
            datetime.min.time(),
        )

        day_end = (
            day_start
            + timedelta(days=1)
        )

        samples = load_system_rows(
            conn,
            day_start,
            day_end,
        )

        if not samples:
            current_date += timedelta(
                days=1
            )
            continue

        pv_energy_wh = integrate_energy(
            samples,
            "total_pv_watts",
        )

        load_energy_wh = integrate_energy(
            samples,
            "total_load_watts",
        )

        charge_samples = []
        discharge_samples = []
        grid_samples = []

        for row in samples:
            row_dict = dict(row)

            battery_watts = (
                row["battery_watts"]
                or 0.0
            )

            grid_watts = (
                row["grid_watts"]
                or 0.0
            )

            charge = dict(row_dict)
            charge["energy_value"] = max(
                battery_watts,
                0.0,
            )

            discharge = dict(row_dict)
            discharge["energy_value"] = max(
                -battery_watts,
                0.0,
            )

            grid = dict(row_dict)
            grid["energy_value"] = max(
                grid_watts,
                0.0,
            )

            charge_samples.append(charge)
            discharge_samples.append(
                discharge
            )
            grid_samples.append(grid)

        pv_samples = [
            r
            for r in samples
            if r["total_pv_watts"]
            is not None
        ]

        load_samples = [
            r
            for r in samples
            if r["total_load_watts"]
            is not None
        ]

        soc_values = [
            r["battery_soc"]
            for r in samples
            if r["battery_soc"]
            is not None
        ]

        pv_peak = (
            max(
                pv_samples,
                key=lambda r:
                    r["total_pv_watts"],
            )
            if pv_samples
            else None
        )

        load_peak = (
            max(
                load_samples,
                key=lambda r:
                    r["total_load_watts"],
            )
            if load_samples
            else None
        )

        production = [
            r
            for r in samples
            if (
                r["total_pv_watts"]
                is not None
                and
                r["total_pv_watts"]
                >= production_threshold
            )
        ]

        production_start = None
        production_end = None
        production_minutes = 0

        if production:
            production_start = (
                production[0]["timestamp"]
            )

            production_end = (
                production[-1]["timestamp"]
            )

            duration = (
                parse_timestamp(
                    production_end
                )
                -
                parse_timestamp(
                    production_start
                )
            )

            production_minutes = max(
                0,
                int(
                    duration.total_seconds()
                    / 60
                ),
            )

        charge_wh = integrate_energy(
            charge_samples,
            "energy_value",
        )

        discharge_wh = integrate_energy(
            discharge_samples,
            "energy_value",
        )

        grid_wh = integrate_energy(
            grid_samples,
            "energy_value",
        )

        equivalent_cycles = None

        if battery_capacity_kwh > 0:
            equivalent_cycles = (
                (
                    charge_wh
                    + discharge_wh
                )
                / 1000.0
                / (
                    2
                    * battery_capacity_kwh
                )
            )

        round_trip_efficiency = None

        if charge_wh > 0:
            round_trip_efficiency = (
                discharge_wh
                / charge_wh
            )

        specific_yield = None
        capacity_factor = None

        if installed_pv_kw > 0:
            specific_yield = (
                pv_energy_wh
                / 1000.0
                / installed_pv_kw
            )

            capacity_factor = (
                pv_energy_wh
                / 1000.0
                / (
                    installed_pv_kw
                    * 24
                )
            )

        conn.execute(
            """
            INSERT INTO daily_summary (
                date,
                pv_energy_wh,
                load_energy_wh,
                battery_charge_wh,
                battery_discharge_wh,
                grid_import_wh,
                pv_peak_watts,
                pv_peak_time,
                load_peak_watts,
                load_peak_time,
                battery_soc_min,
                battery_soc_max,
                production_start,
                production_end,
                production_minutes,
                equivalent_battery_cycles,
                battery_round_trip_efficiency,
                specific_yield_kwh_per_kw,
                capacity_factor
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(date)
            DO UPDATE SET
                pv_energy_wh =
                    excluded.pv_energy_wh,
                load_energy_wh =
                    excluded.load_energy_wh,
                battery_charge_wh =
                    excluded.battery_charge_wh,
                battery_discharge_wh =
                    excluded.battery_discharge_wh,
                grid_import_wh =
                    excluded.grid_import_wh,
                pv_peak_watts =
                    excluded.pv_peak_watts,
                pv_peak_time =
                    excluded.pv_peak_time,
                load_peak_watts =
                    excluded.load_peak_watts,
                load_peak_time =
                    excluded.load_peak_time,
                battery_soc_min =
                    excluded.battery_soc_min,
                battery_soc_max =
                    excluded.battery_soc_max,
                production_start =
                    excluded.production_start,
                production_end =
                    excluded.production_end,
                production_minutes =
                    excluded.production_minutes,
                equivalent_battery_cycles =
                    excluded.equivalent_battery_cycles,
                battery_round_trip_efficiency =
                    excluded.battery_round_trip_efficiency,
                specific_yield_kwh_per_kw =
                    excluded.specific_yield_kwh_per_kw,
                capacity_factor =
                    excluded.capacity_factor
            """,
            (
                current_date.isoformat(),
                pv_energy_wh,
                load_energy_wh,
                charge_wh,
                discharge_wh,
                grid_wh,
                (
                    pv_peak[
                        "total_pv_watts"
                    ]
                    if pv_peak
                    else None
                ),
                (
                    pv_peak["timestamp"]
                    if pv_peak
                    else None
                ),
                (
                    load_peak[
                        "total_load_watts"
                    ]
                    if load_peak
                    else None
                ),
                (
                    load_peak[
                        "timestamp"
                    ]
                    if load_peak
                    else None
                ),
                minimum(soc_values),
                maximum(soc_values),
                production_start,
                production_end,
                production_minutes,
                equivalent_cycles,
                round_trip_efficiency,
                specific_yield,
                capacity_factor,
            ),
        )

        conn.commit()

        count += 1
        current_date += timedelta(days=1)

    return count



def aggregate_daily_inverters(conn):
    """
    Build completed per-inverter daily summaries.

    On the first catch-up run, resume from the newest existing
    inverter summary with a one-day overlap. After catch-up,
    this only recalculates the most recent completed days.
    """

    today = datetime.now().date()

    raw_oldest = conn.execute(
        """
        SELECT MIN(timestamp)
        FROM inverter_samples
        """
    ).fetchone()[0]

    if raw_oldest is None:
        return 0

    raw_start = (
        parse_timestamp(raw_oldest).date()
    )

    latest_summary = conn.execute(
        """
        SELECT MAX(date)
        FROM daily_inverter_summary
        """
    ).fetchone()[0]

    if latest_summary is None:
        current_date = raw_start
    else:
        current_date = (
            datetime.fromisoformat(
                latest_summary
            ).date()
            - DAILY_OVERLAP
        )

        if current_date < raw_start:
            current_date = raw_start

    count = 0

    while current_date < today:
        next_date = (
            current_date
            + timedelta(days=1)
        )

        start_timestamp = (
            current_date.isoformat()
            + "T00:00:00"
        )

        end_timestamp = (
            next_date.isoformat()
            + "T00:00:00"
        )

        rows = conn.execute(
            """
            SELECT
                timestamp,
                inverter_id,
                pv_watts,
                output_watts,
                inverter_temperature_c,
                status
            FROM inverter_samples
            WHERE timestamp >= ?
              AND timestamp < ?
            ORDER BY
                inverter_id,
                timestamp
            """,
            (
                start_timestamp,
                end_timestamp,
            ),
        ).fetchall()

        groups = defaultdict(list)

        for row in rows:
            groups[
                row["inverter_id"]
            ].append(row)

        for inverter_id, samples in groups.items():
            pv_energy_wh = integrate_energy(
                samples,
                "pv_watts",
            )

            load_energy_wh = integrate_energy(
                samples,
                "output_watts",
            )

            pv_samples = [
                row
                for row in samples
                if row["pv_watts"]
                is not None
            ]

            load_samples = [
                row
                for row in samples
                if row["output_watts"]
                is not None
            ]

            temperature_values = [
                row["inverter_temperature_c"]
                for row in samples
                if (
                    row[
                        "inverter_temperature_c"
                    ]
                    is not None
                )
            ]

            pv_peak = (
                max(
                    pv_samples,
                    key=lambda row:
                        row["pv_watts"],
                )
                if pv_samples
                else None
            )

            load_peak = (
                max(
                    load_samples,
                    key=lambda row:
                        row["output_watts"],
                )
                if load_samples
                else None
            )

            # Current collector status strings such as
            #
            # eg4_serial_parallel:B:pv_measured
            # eg4_serial_parallel:B:pv_estimated
            #
            # describe operating state / PV quality and are
            # not fault reports.
            fault_count = 0

            for row in samples:
                status = row["status"]

                if status in (
                    None,
                    "",
                    "mock",
                    "normal",
                ):
                    continue

                if (
                    isinstance(status, str)
                    and status.startswith(
                        "eg4_serial"
                    )
                ):
                    continue

                fault_count += 1

            conn.execute(
                """
                INSERT INTO daily_inverter_summary (
                    date,
                    inverter_id,
                    pv_energy_wh,
                    load_energy_wh,
                    peak_pv_watts,
                    peak_pv_time,
                    peak_load_watts,
                    peak_load_time,
                    peak_temperature_c,
                    avg_temperature_c,
                    fault_count
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )

                ON CONFLICT(date, inverter_id)
                DO UPDATE SET
                    pv_energy_wh =
                        excluded.pv_energy_wh,
                    load_energy_wh =
                        excluded.load_energy_wh,
                    peak_pv_watts =
                        excluded.peak_pv_watts,
                    peak_pv_time =
                        excluded.peak_pv_time,
                    peak_load_watts =
                        excluded.peak_load_watts,
                    peak_load_time =
                        excluded.peak_load_time,
                    peak_temperature_c =
                        excluded.peak_temperature_c,
                    avg_temperature_c =
                        excluded.avg_temperature_c,
                    fault_count =
                        excluded.fault_count
                """,
                (
                    current_date.isoformat(),
                    inverter_id,
                    pv_energy_wh,
                    load_energy_wh,
                    (
                        pv_peak["pv_watts"]
                        if pv_peak
                        else None
                    ),
                    (
                        pv_peak["timestamp"]
                        if pv_peak
                        else None
                    ),
                    (
                        load_peak["output_watts"]
                        if load_peak
                        else None
                    ),
                    (
                        load_peak["timestamp"]
                        if load_peak
                        else None
                    ),
                    maximum(
                        temperature_values
                    ),
                    average(
                        temperature_values
                    ),
                    fault_count,
                ),
            )

            count += 1

        # Keep the writer transaction short.
        conn.commit()

        current_date = next_date

    return count

def aggregate_monthly(conn):
    rows = conn.execute(
        """
        SELECT *
        FROM daily_summary
        ORDER BY date
        """
    ).fetchall()

    groups = defaultdict(list)

    for row in rows:
        groups[
            row["date"][:7]
        ].append(row)

    count = 0

    for month, samples in groups.items():
        pv = sum(
            r["pv_energy_wh"] or 0.0
            for r in samples
        )

        load = sum(
            r["load_energy_wh"] or 0.0
            for r in samples
        )

        charge = sum(
            r["battery_charge_wh"] or 0.0
            for r in samples
        )

        discharge = sum(
            r["battery_discharge_wh"] or 0.0
            for r in samples
        )

        grid = sum(
            r["grid_import_wh"] or 0.0
            for r in samples
        )

        cycles = sum(
            r["equivalent_battery_cycles"]
            or 0.0
            for r in samples
        )

        pv_peaks = [
            r["pv_peak_watts"]
            for r in samples
            if r["pv_peak_watts"]
            is not None
        ]

        load_peaks = [
            r["load_peak_watts"]
            for r in samples
            if r["load_peak_watts"]
            is not None
        ]

        soc_min = [
            r["battery_soc_min"]
            for r in samples
            if r["battery_soc_min"]
            is not None
        ]

        days = len(samples)

        conn.execute(
            """
            INSERT INTO monthly_summary (
                month,
                pv_energy_wh,
                load_energy_wh,
                battery_charge_wh,
                battery_discharge_wh,
                grid_import_wh,
                peak_pv_watts,
                peak_load_watts,
                avg_daily_pv_wh,
                avg_daily_load_wh,
                minimum_battery_soc,
                equivalent_battery_cycles,
                days_recorded
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(month)
            DO UPDATE SET
                pv_energy_wh =
                    excluded.pv_energy_wh,
                load_energy_wh =
                    excluded.load_energy_wh,
                battery_charge_wh =
                    excluded.battery_charge_wh,
                battery_discharge_wh =
                    excluded.battery_discharge_wh,
                grid_import_wh =
                    excluded.grid_import_wh,
                peak_pv_watts =
                    excluded.peak_pv_watts,
                peak_load_watts =
                    excluded.peak_load_watts,
                avg_daily_pv_wh =
                    excluded.avg_daily_pv_wh,
                avg_daily_load_wh =
                    excluded.avg_daily_load_wh,
                minimum_battery_soc =
                    excluded.minimum_battery_soc,
                equivalent_battery_cycles =
                    excluded.equivalent_battery_cycles,
                days_recorded =
                    excluded.days_recorded
            """,
            (
                month,
                pv,
                load,
                charge,
                discharge,
                grid,
                maximum(pv_peaks),
                maximum(load_peaks),
                pv / days,
                load / days,
                minimum(soc_min),
                cycles,
                days,
            ),
        )

        count += 1

    conn.commit()

    return count


def aggregate_yearly(conn):
    rows = conn.execute(
        """
        SELECT *
        FROM daily_summary
        ORDER BY date
        """
    ).fetchall()

    groups = defaultdict(list)

    for row in rows:
        groups[
            int(row["date"][:4])
        ].append(row)

    count = 0

    for year, samples in groups.items():
        pv = sum(
            r["pv_energy_wh"] or 0.0
            for r in samples
        )

        load = sum(
            r["load_energy_wh"] or 0.0
            for r in samples
        )

        charge = sum(
            r["battery_charge_wh"] or 0.0
            for r in samples
        )

        discharge = sum(
            r["battery_discharge_wh"] or 0.0
            for r in samples
        )

        grid = sum(
            r["grid_import_wh"] or 0.0
            for r in samples
        )

        cycles = sum(
            r["equivalent_battery_cycles"]
            or 0.0
            for r in samples
        )

        pv_peaks = [
            r["pv_peak_watts"]
            for r in samples
            if r["pv_peak_watts"]
            is not None
        ]

        load_peaks = [
            r["load_peak_watts"]
            for r in samples
            if r["load_peak_watts"]
            is not None
        ]

        soc_min = [
            r["battery_soc_min"]
            for r in samples
            if r["battery_soc_min"]
            is not None
        ]

        days = len(samples)

        conn.execute(
            """
            INSERT INTO yearly_summary (
                year,
                pv_energy_wh,
                load_energy_wh,
                battery_charge_wh,
                battery_discharge_wh,
                grid_import_wh,
                peak_pv_watts,
                peak_load_watts,
                average_daily_pv_wh,
                average_daily_load_wh,
                minimum_battery_soc,
                equivalent_battery_cycles,
                days_recorded
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(year)
            DO UPDATE SET
                pv_energy_wh =
                    excluded.pv_energy_wh,
                load_energy_wh =
                    excluded.load_energy_wh,
                battery_charge_wh =
                    excluded.battery_charge_wh,
                battery_discharge_wh =
                    excluded.battery_discharge_wh,
                grid_import_wh =
                    excluded.grid_import_wh,
                peak_pv_watts =
                    excluded.peak_pv_watts,
                peak_load_watts =
                    excluded.peak_load_watts,
                average_daily_pv_wh =
                    excluded.average_daily_pv_wh,
                average_daily_load_wh =
                    excluded.average_daily_load_wh,
                minimum_battery_soc =
                    excluded.minimum_battery_soc,
                equivalent_battery_cycles =
                    excluded.equivalent_battery_cycles,
                days_recorded =
                    excluded.days_recorded
            """,
            (
                year,
                pv,
                load,
                charge,
                discharge,
                grid,
                maximum(pv_peaks),
                maximum(load_peaks),
                pv / days,
                load / days,
                minimum(soc_min),
                cycles,
                days,
            ),
        )

        count += 1

    conn.commit()

    return count



def prune_old_data(conn):
    """
    Retention policy:

    - raw system/inverter samples: 7 days
    - minute summaries: 365 days
    - quarter-hour and longer summaries: indefinite

    Raw samples are deleted only after daily summary coverage
    has been verified.
    """

    now = datetime.now()

    raw_cutoff = (
        now - timedelta(days=7)
    ).isoformat(timespec="seconds")

    raw_cutoff_date = raw_cutoff[:10]

    minute_cutoff = (
        now - timedelta(days=365)
    ).isoformat(timespec="seconds")

    raw_system_days = {
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT
                substr(timestamp, 1, 10)
            FROM system_samples
            WHERE timestamp < ?
            """,
            (raw_cutoff,),
        )
    }

    system_summary_days = {
        row[0]
        for row in conn.execute(
            """
            SELECT date
            FROM daily_summary
            WHERE date < ?
            """,
            (raw_cutoff_date,),
        )
    }

    missing_system = (
        raw_system_days
        - system_summary_days
    )

    if missing_system:
        raise RuntimeError(
            "Retention aborted: "
            "missing system daily summaries for "
            + ", ".join(
                sorted(missing_system)
            )
        )

    inverter_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT inverter_id
            FROM inverter_samples
            ORDER BY inverter_id
            """
        )
    ]

    for inverter_id in inverter_ids:
        raw_days = {
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT
                    substr(timestamp, 1, 10)
                FROM inverter_samples
                WHERE inverter_id = ?
                  AND timestamp < ?
                """,
                (
                    inverter_id,
                    raw_cutoff,
                ),
            )
        }

        summary_days = {
            row[0]
            for row in conn.execute(
                """
                SELECT date
                FROM daily_inverter_summary
                WHERE inverter_id = ?
                  AND date < ?
                """,
                (
                    inverter_id,
                    raw_cutoff_date,
                ),
            )
        }

        missing = (
            raw_days
            - summary_days
        )

        if missing:
            raise RuntimeError(
                "Retention aborted: "
                f"inverter {inverter_id} "
                "is missing daily summaries for "
                + ", ".join(
                    sorted(missing)
                )
            )

    system_before = conn.total_changes

    with conn:
        system_cursor = conn.execute(
            """
            DELETE FROM system_samples
            WHERE timestamp < ?
            """,
            (raw_cutoff,),
        )

        inverter_cursor = conn.execute(
            """
            DELETE FROM inverter_samples
            WHERE timestamp < ?
            """,
            (raw_cutoff,),
        )

        minute_cursor = conn.execute(
            """
            DELETE FROM minute_summary
            WHERE timestamp < ?
            """,
            (minute_cutoff,),
        )

    return {
        "system_samples":
            system_cursor.rowcount,
        "inverter_samples":
            inverter_cursor.rowcount,
        "minute_summary":
            minute_cursor.rowcount,
    }

def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist"
        )

    print(
        "Running incremental solar aggregation..."
    )

    conn = connect_db()

    try:
        count = aggregate_minutes(conn)
        print(
            f"Minute buckets updated: {count}"
        )

        count = aggregate_quarter_hours(
            conn
        )
        print(
            f"15-minute buckets updated: {count}"
        )

        count = aggregate_daily(conn)
        print(
            f"Daily summaries updated: {count}"
        )

        count = aggregate_daily_inverters(
            conn
        )
        print(
            f"Daily inverter summaries updated: {count}"
        )

        count = aggregate_monthly(conn)
        print(
            f"Monthly summaries updated: {count}"
        )

        count = aggregate_yearly(conn)
        print(
            f"Yearly summaries updated: {count}"
        )

        deleted = prune_old_data(conn)
        print(
            "Retention pruned: "
            f"{deleted['system_samples']} system, "
            f"{deleted['inverter_samples']} inverter, "
            f"{deleted['minute_summary']} minute"
        )

    finally:
        conn.close()

    print(
        "Incremental aggregation complete."
    )


if __name__ == "__main__":
    main()
