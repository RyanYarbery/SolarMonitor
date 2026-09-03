import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("solar_v1.db")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    return conn


def parse_timestamp(value):
    return datetime.fromisoformat(value)


def floor_minute(dt):
    return dt.replace(second=0, microsecond=0)


def floor_quarter_hour(dt):
    minute = (dt.minute // 15) * 15

    return dt.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )


def average(values):
    values = [v for v in values if v is not None]

    if not values:
        return None

    return sum(values) / len(values)


def minimum(values):
    values = [v for v in values if v is not None]

    if not values:
        return None

    return min(values)


def maximum(values):
    values = [v for v in values if v is not None]

    if not values:
        return None

    return max(values)


def integrate_energy(samples, key):
    """
    Integrate power readings into watt-hours using the actual
    time difference between samples.

    Uses trapezoidal integration.
    """

    valid = [
        sample
        for sample in samples
        if sample[key] is not None
    ]

    if len(valid) < 2:
        return 0.0

    total_wh = 0.0

    for previous, current in zip(valid, valid[1:]):
        t1 = parse_timestamp(previous["timestamp"])
        t2 = parse_timestamp(current["timestamp"])

        seconds = (t2 - t1).total_seconds()

        if seconds <= 0:
            continue

        # Reject unusually large gaps.
        if seconds > 60:
            continue

        p1 = float(previous[key])
        p2 = float(current[key])

        average_watts = (p1 + p2) / 2.0

        total_wh += average_watts * seconds / 3600.0

    return total_wh


def aggregate_minutes(conn):
    rows = conn.execute("""
        SELECT
            timestamp,
            total_pv_watts,
            total_load_watts,
            battery_soc,
            battery_watts,
            grid_watts
        FROM system_samples
        ORDER BY timestamp
    """).fetchall()

    groups = defaultdict(list)

    for row in rows:
        dt = parse_timestamp(row["timestamp"])
        bucket = floor_minute(dt).isoformat(timespec="seconds")
        groups[bucket].append(row)

    for timestamp, samples in groups.items():

        pv = [r["total_pv_watts"] for r in samples]
        load = [r["total_load_watts"] for r in samples]
        soc = [r["battery_soc"] for r in samples]
        battery = [r["battery_watts"] for r in samples]
        grid = [r["grid_watts"] for r in samples]

        conn.execute("""
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(timestamp)
            DO UPDATE SET
                pv_watts_avg = excluded.pv_watts_avg,
                pv_watts_min = excluded.pv_watts_min,
                pv_watts_max = excluded.pv_watts_max,
                load_watts_avg = excluded.load_watts_avg,
                load_watts_min = excluded.load_watts_min,
                load_watts_max = excluded.load_watts_max,
                battery_soc_avg = excluded.battery_soc_avg,
                battery_soc_min = excluded.battery_soc_min,
                battery_soc_max = excluded.battery_soc_max,
                battery_watts_avg = excluded.battery_watts_avg,
                grid_watts_avg = excluded.grid_watts_avg,
                sample_count = excluded.sample_count
        """, (
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
        ))


def aggregate_quarter_hours(conn):
    rows = conn.execute("""
        SELECT
            timestamp,
            total_pv_watts,
            total_load_watts,
            battery_soc,
            battery_watts,
            grid_watts
        FROM system_samples
        ORDER BY timestamp
    """).fetchall()

    groups = defaultdict(list)

    for row in rows:
        dt = parse_timestamp(row["timestamp"])
        bucket = floor_quarter_hour(dt).isoformat(timespec="seconds")
        groups[bucket].append(row)

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

            battery_watts = row["battery_watts"] or 0.0
            grid_watts = row["grid_watts"] or 0.0

            charge_row = dict(row_dict)
            charge_row["energy_value"] = max(
                battery_watts,
                0.0,
            )

            discharge_row = dict(row_dict)
            discharge_row["energy_value"] = max(
                -battery_watts,
                0.0,
            )

            grid_row = dict(row_dict)
            grid_row["energy_value"] = max(
                grid_watts,
                0.0,
            )

            charge_samples.append(charge_row)
            discharge_samples.append(discharge_row)
            grid_samples.append(grid_row)

        battery_charge_wh = integrate_energy(
            charge_samples,
            "energy_value",
        )

        battery_discharge_wh = integrate_energy(
            discharge_samples,
            "energy_value",
        )

        grid_import_wh = integrate_energy(
            grid_samples,
            "energy_value",
        )

        pv_values = [
            r["total_pv_watts"]
            for r in samples
            if r["total_pv_watts"] is not None
        ]

        load_values = [
            r["total_load_watts"]
            for r in samples
            if r["total_load_watts"] is not None
        ]

        soc_values = [
            r["battery_soc"]
            for r in samples
            if r["battery_soc"] is not None
        ]

        conn.execute("""
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(timestamp)
            DO UPDATE SET
                pv_energy_wh = excluded.pv_energy_wh,
                load_energy_wh = excluded.load_energy_wh,
                battery_charge_wh = excluded.battery_charge_wh,
                battery_discharge_wh = excluded.battery_discharge_wh,
                grid_import_wh = excluded.grid_import_wh,
                pv_peak_watts = excluded.pv_peak_watts,
                load_peak_watts = excluded.load_peak_watts,
                battery_soc_min = excluded.battery_soc_min,
                battery_soc_max = excluded.battery_soc_max,
                sample_count = excluded.sample_count
        """, (
            timestamp,
            pv_energy_wh,
            load_energy_wh,
            battery_charge_wh,
            battery_discharge_wh,
            grid_import_wh,
            max(pv_values) if pv_values else None,
            max(load_values) if load_values else None,
            min(soc_values) if soc_values else None,
            max(soc_values) if soc_values else None,
            len(samples),
        ))


def aggregate_daily_system(conn):
    rows = conn.execute("""
        SELECT
            timestamp,
            total_pv_watts,
            total_load_watts,
            battery_soc,
            battery_watts,
            grid_watts
        FROM system_samples
        ORDER BY timestamp
    """).fetchall()

    groups = defaultdict(list)

    for row in rows:
        dt = parse_timestamp(row["timestamp"])
        groups[dt.date().isoformat()].append(row)

    config = dict(
        conn.execute(
            "SELECT key, value FROM system_config"
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

    for date, samples in groups.items():

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

            battery_watts = row["battery_watts"] or 0.0
            grid_watts = row["grid_watts"] or 0.0

            charge_row = dict(row_dict)
            charge_row["energy_value"] = max(
                battery_watts,
                0.0,
            )

            discharge_row = dict(row_dict)
            discharge_row["energy_value"] = max(
                -battery_watts,
                0.0,
            )

            grid_row = dict(row_dict)
            grid_row["energy_value"] = max(
                grid_watts,
                0.0,
            )

            charge_samples.append(charge_row)
            discharge_samples.append(discharge_row)
            grid_samples.append(grid_row)

        battery_charge_wh = integrate_energy(
            charge_samples,
            "energy_value",
        )

        battery_discharge_wh = integrate_energy(
            discharge_samples,
            "energy_value",
        )

        grid_import_wh = integrate_energy(
            grid_samples,
            "energy_value",
        )

        pv_samples = [
            row
            for row in samples
            if row["total_pv_watts"] is not None
        ]

        load_samples = [
            row
            for row in samples
            if row["total_load_watts"] is not None
        ]

        soc_values = [
            row["battery_soc"]
            for row in samples
            if row["battery_soc"] is not None
        ]

        pv_peak_row = max(
            pv_samples,
            key=lambda x: x["total_pv_watts"],
        ) if pv_samples else None

        load_peak_row = max(
            load_samples,
            key=lambda x: x["total_load_watts"],
        ) if load_samples else None

        production_samples = [
            row
            for row in samples
            if (
                row["total_pv_watts"] is not None
                and row["total_pv_watts"]
                >= production_threshold
            )
        ]

        production_start = None
        production_end = None
        production_minutes = 0

        if production_samples:

            production_start = production_samples[0][
                "timestamp"
            ]

            production_end = production_samples[-1][
                "timestamp"
            ]

            start = parse_timestamp(production_start)
            end = parse_timestamp(production_end)

            production_minutes = max(
                0,
                int(
                    (end - start).total_seconds()
                    / 60
                ),
            )

        equivalent_cycles = None

        if battery_capacity_kwh > 0:

            throughput_kwh = (
                battery_charge_wh
                + battery_discharge_wh
            ) / 1000.0

            equivalent_cycles = (
                throughput_kwh
                / (2 * battery_capacity_kwh)
            )

        round_trip_efficiency = None

        if battery_charge_wh > 0:

            round_trip_efficiency = (
                battery_discharge_wh
                / battery_charge_wh
            )

        specific_yield = None

        if installed_pv_kw > 0:

            specific_yield = (
                pv_energy_wh
                / 1000.0
                / installed_pv_kw
            )

        capacity_factor = None

        if installed_pv_kw > 0:

            capacity_factor = (
                pv_energy_wh / 1000.0
            ) / (
                installed_pv_kw * 24
            )

        conn.execute("""
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(date)
            DO UPDATE SET
                pv_energy_wh = excluded.pv_energy_wh,
                load_energy_wh = excluded.load_energy_wh,
                battery_charge_wh = excluded.battery_charge_wh,
                battery_discharge_wh = excluded.battery_discharge_wh,
                grid_import_wh = excluded.grid_import_wh,
                pv_peak_watts = excluded.pv_peak_watts,
                pv_peak_time = excluded.pv_peak_time,
                load_peak_watts = excluded.load_peak_watts,
                load_peak_time = excluded.load_peak_time,
                battery_soc_min = excluded.battery_soc_min,
                battery_soc_max = excluded.battery_soc_max,
                production_start = excluded.production_start,
                production_end = excluded.production_end,
                production_minutes = excluded.production_minutes,
                equivalent_battery_cycles = excluded.equivalent_battery_cycles,
                battery_round_trip_efficiency = excluded.battery_round_trip_efficiency,
                specific_yield_kwh_per_kw = excluded.specific_yield_kwh_per_kw,
                capacity_factor = excluded.capacity_factor
        """, (
            date,
            pv_energy_wh,
            load_energy_wh,
            battery_charge_wh,
            battery_discharge_wh,
            grid_import_wh,

            (
                pv_peak_row["total_pv_watts"]
                if pv_peak_row
                else None
            ),

            (
                pv_peak_row["timestamp"]
                if pv_peak_row
                else None
            ),

            (
                load_peak_row["total_load_watts"]
                if load_peak_row
                else None
            ),

            (
                load_peak_row["timestamp"]
                if load_peak_row
                else None
            ),

            min(soc_values) if soc_values else None,
            max(soc_values) if soc_values else None,

            production_start,
            production_end,
            production_minutes,

            equivalent_cycles,
            round_trip_efficiency,
            specific_yield,
            capacity_factor,
        ))


def aggregate_daily_inverters(conn):
    rows = conn.execute("""
        SELECT
            timestamp,
            inverter_id,
            pv_watts,
            output_watts,
            inverter_temperature_c,
            status
        FROM inverter_samples
        ORDER BY timestamp
    """).fetchall()

    groups = defaultdict(list)

    for row in rows:
        dt = parse_timestamp(row["timestamp"])

        key = (
            dt.date().isoformat(),
            row["inverter_id"],
        )

        groups[key].append(row)

    for (date, inverter_id), samples in groups.items():

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
            if row["pv_watts"] is not None
        ]

        load_samples = [
            row
            for row in samples
            if row["output_watts"] is not None
        ]

        temperature_values = [
            row["inverter_temperature_c"]
            for row in samples
            if row["inverter_temperature_c"]
            is not None
        ]

        pv_peak_row = max(
            pv_samples,
            key=lambda x: x["pv_watts"],
        ) if pv_samples else None

        load_peak_row = max(
            load_samples,
            key=lambda x: x["output_watts"],
        ) if load_samples else None

        fault_count = sum(
            1
            for row in samples
            if row["status"]
            not in (
                None,
                "",
                "mock",
                "normal",
            )
        )

        conn.execute("""
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(date, inverter_id)
            DO UPDATE SET
                pv_energy_wh = excluded.pv_energy_wh,
                load_energy_wh = excluded.load_energy_wh,
                peak_pv_watts = excluded.peak_pv_watts,
                peak_pv_time = excluded.peak_pv_time,
                peak_load_watts = excluded.peak_load_watts,
                peak_load_time = excluded.peak_load_time,
                peak_temperature_c = excluded.peak_temperature_c,
                avg_temperature_c = excluded.avg_temperature_c,
                fault_count = excluded.fault_count
        """, (
            date,
            inverter_id,
            pv_energy_wh,
            load_energy_wh,

            (
                pv_peak_row["pv_watts"]
                if pv_peak_row
                else None
            ),

            (
                pv_peak_row["timestamp"]
                if pv_peak_row
                else None
            ),

            (
                load_peak_row["output_watts"]
                if load_peak_row
                else None
            ),

            (
                load_peak_row["timestamp"]
                if load_peak_row
                else None
            ),

            maximum(temperature_values),
            average(temperature_values),
            fault_count,
        ))


def aggregate_monthly(conn):
    rows = conn.execute("""
        SELECT *
        FROM daily_summary
        ORDER BY date
    """).fetchall()

    groups = defaultdict(list)

    for row in rows:
        month = row["date"][:7]
        groups[month].append(row)

    for month, samples in groups.items():

        pv_energy = sum(
            row["pv_energy_wh"] or 0.0
            for row in samples
        )

        load_energy = sum(
            row["load_energy_wh"] or 0.0
            for row in samples
        )

        charge_energy = sum(
            row["battery_charge_wh"] or 0.0
            for row in samples
        )

        discharge_energy = sum(
            row["battery_discharge_wh"] or 0.0
            for row in samples
        )

        grid_energy = sum(
            row["grid_import_wh"] or 0.0
            for row in samples
        )

        cycles = sum(
            row["equivalent_battery_cycles"] or 0.0
            for row in samples
        )

        pv_peaks = [
            row["pv_peak_watts"]
            for row in samples
            if row["pv_peak_watts"] is not None
        ]

        load_peaks = [
            row["load_peak_watts"]
            for row in samples
            if row["load_peak_watts"] is not None
        ]

        soc_min = [
            row["battery_soc_min"]
            for row in samples
            if row["battery_soc_min"] is not None
        ]

        specific_yields = [
            row["specific_yield_kwh_per_kw"]
            for row in samples
            if row["specific_yield_kwh_per_kw"]
            is not None
        ]

        capacity_factors = [
            row["capacity_factor"]
            for row in samples
            if row["capacity_factor"] is not None
        ]

        days = len(samples)

        conn.execute("""
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
                specific_yield_kwh_per_kw,
                capacity_factor,
                days_recorded
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(month)
            DO UPDATE SET
                pv_energy_wh = excluded.pv_energy_wh,
                load_energy_wh = excluded.load_energy_wh,
                battery_charge_wh = excluded.battery_charge_wh,
                battery_discharge_wh = excluded.battery_discharge_wh,
                grid_import_wh = excluded.grid_import_wh,
                peak_pv_watts = excluded.peak_pv_watts,
                peak_load_watts = excluded.peak_load_watts,
                avg_daily_pv_wh = excluded.avg_daily_pv_wh,
                avg_daily_load_wh = excluded.avg_daily_load_wh,
                minimum_battery_soc = excluded.minimum_battery_soc,
                equivalent_battery_cycles = excluded.equivalent_battery_cycles,
                specific_yield_kwh_per_kw = excluded.specific_yield_kwh_per_kw,
                capacity_factor = excluded.capacity_factor,
                days_recorded = excluded.days_recorded
        """, (
            month,

            pv_energy,
            load_energy,

            charge_energy,
            discharge_energy,

            grid_energy,

            max(pv_peaks) if pv_peaks else None,
            max(load_peaks) if load_peaks else None,

            pv_energy / days if days else None,
            load_energy / days if days else None,

            min(soc_min) if soc_min else None,

            cycles,

            (
                sum(specific_yields)
                if specific_yields
                else None
            ),

            (
                average(capacity_factors)
                if capacity_factors
                else None
            ),

            days,
        ))


def aggregate_yearly(conn):
    rows = conn.execute("""
        SELECT *
        FROM daily_summary
        ORDER BY date
    """).fetchall()

    groups = defaultdict(list)

    for row in rows:
        year = int(row["date"][:4])
        groups[year].append(row)

    for year, samples in groups.items():

        pv_energy = sum(
            row["pv_energy_wh"] or 0.0
            for row in samples
        )

        load_energy = sum(
            row["load_energy_wh"] or 0.0
            for row in samples
        )

        charge_energy = sum(
            row["battery_charge_wh"] or 0.0
            for row in samples
        )

        discharge_energy = sum(
            row["battery_discharge_wh"] or 0.0
            for row in samples
        )

        grid_energy = sum(
            row["grid_import_wh"] or 0.0
            for row in samples
        )

        cycles = sum(
            row["equivalent_battery_cycles"] or 0.0
            for row in samples
        )

        pv_peaks = [
            row["pv_peak_watts"]
            for row in samples
            if row["pv_peak_watts"] is not None
        ]

        load_peaks = [
            row["load_peak_watts"]
            for row in samples
            if row["load_peak_watts"] is not None
        ]

        soc_min = [
            row["battery_soc_min"]
            for row in samples
            if row["battery_soc_min"] is not None
        ]

        specific_yields = [
            row["specific_yield_kwh_per_kw"]
            for row in samples
            if row["specific_yield_kwh_per_kw"]
            is not None
        ]

        capacity_factors = [
            row["capacity_factor"]
            for row in samples
            if row["capacity_factor"] is not None
        ]

        days = len(samples)

        conn.execute("""
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
                specific_yield_kwh_per_kw,
                capacity_factor,
                days_recorded
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(year)
            DO UPDATE SET
                pv_energy_wh = excluded.pv_energy_wh,
                load_energy_wh = excluded.load_energy_wh,
                battery_charge_wh = excluded.battery_charge_wh,
                battery_discharge_wh = excluded.battery_discharge_wh,
                grid_import_wh = excluded.grid_import_wh,
                peak_pv_watts = excluded.peak_pv_watts,
                peak_load_watts = excluded.peak_load_watts,
                average_daily_pv_wh = excluded.average_daily_pv_wh,
                average_daily_load_wh = excluded.average_daily_load_wh,
                minimum_battery_soc = excluded.minimum_battery_soc,
                equivalent_battery_cycles = excluded.equivalent_battery_cycles,
                specific_yield_kwh_per_kw = excluded.specific_yield_kwh_per_kw,
                capacity_factor = excluded.capacity_factor,
                days_recorded = excluded.days_recorded
        """, (
            year,

            pv_energy,
            load_energy,

            charge_energy,
            discharge_energy,

            grid_energy,

            max(pv_peaks) if pv_peaks else None,
            max(load_peaks) if load_peaks else None,

            pv_energy / days if days else None,
            load_energy / days if days else None,

            min(soc_min) if soc_min else None,

            cycles,

            (
                sum(specific_yields)
                if specific_yields
                else None
            ),

            (
                average(capacity_factors)
                if capacity_factors
                else None
            ),

            days,
        ))


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist."
        )

    print("Running solar aggregation...")

    conn = connect_db()

    try:
        aggregate_minutes(conn)
        print("Minute summaries complete.")

        aggregate_quarter_hours(conn)
        print("15-minute summaries complete.")

        aggregate_daily_system(conn)
        print("Daily system summaries complete.")

        aggregate_daily_inverters(conn)
        print("Daily inverter summaries complete.")

        aggregate_monthly(conn)
        print("Monthly summaries complete.")

        aggregate_yearly(conn)
        print("Yearly summaries complete.")

        conn.commit()

    finally:
        conn.close()

    print("Aggregation complete.")


if __name__ == "__main__":
    main()
