import json
import math
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from eg4 import get_all_parallel_status, get_status, get_pv2_status

DB_PATH = Path("solar_v1.db")
LIVE_PATH = Path("/run/solar-monitor/latest.json")

SAMPLE_INTERVAL = 10



def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def get_mock_inverter_readings():
    now = datetime.now()
    hour = now.hour + now.minute / 60 + now.second / 3600

    daylight_start = 6.0
    daylight_end = 20.0

    if daylight_start <= hour <= daylight_end:
        progress = (hour - daylight_start) / (
            daylight_end - daylight_start
        )
        solar_curve = max(
            0.0,
            math.sin(math.pi * progress),
        )
    else:
        solar_curve = 0.0

    readings = []

    for inverter_id in (1, 2):
        pv_watts = (
            solar_curve
            * 5500
            * random.uniform(0.93, 1.07)
            * random.uniform(0.85, 1.05)
        )

        if pv_watts < 20:
            pv_watts = 0.0

        load_watts = random.uniform(700, 1800)
        battery_voltage = random.uniform(50.5, 53.5)
        battery_soc = random.uniform(55, 95)

        battery_charge_amps = max(
            0.0,
            (pv_watts - load_watts)
            / max(battery_voltage, 1),
        )

        battery_discharge_amps = max(
            0.0,
            (load_watts - pv_watts)
            / max(battery_voltage, 1),
        )

        inverter_temperature_c = (
            31
            + (pv_watts / 5500) * 18
            + random.uniform(-2, 2)
        )

        pv_voltage = (
            random.uniform(280, 390)
            if pv_watts > 0
            else 0.0
        )

        pv_current = (
            pv_watts / pv_voltage
            if pv_voltage > 0
            else 0.0
        )

        readings.append(
            {
                "inverter_id": inverter_id,
                "grid_voltage": 0.0,
                "grid_frequency": 0.0,
                "output_voltage": random.uniform(118, 122),
                "output_frequency": random.uniform(59.9, 60.1),
                "output_va": (
                    load_watts
                    * random.uniform(1.00, 1.08)
                ),
                "output_watts": load_watts,
                "load_percent": (
                    load_watts / 6500
                ) * 100,
                "battery_voltage": battery_voltage,
                "battery_charge_amps": battery_charge_amps,
                "battery_discharge_amps": battery_discharge_amps,
                "battery_soc": battery_soc,
                "inverter_temperature_c": inverter_temperature_c,
                "pv_voltage": pv_voltage,
                "pv_current": pv_current,
                "pv_watts": pv_watts,
                "status": "mock",
            }
        )

    return readings



def get_real_inverter_readings():
    # QPGS0/QPGS1 expose both parallel inverters.
    statuses = get_all_parallel_status()

    # The serial cable is physically attached to QPGS index 0.
    # QPIGS gives direct PV1 charging power and QPIGS2 gives
    # direct PV2 charging power for this inverter.
    direct_status = get_status()
    direct_pv2 = get_pv2_status()

    direct_pv_watts = (
        direct_status["pv_charging_power"]
        + direct_pv2["pv2_watts"]
    )

    readings = []

    for inverter_index, status in enumerate(statuses):
        inverter_id = inverter_index + 1

        pv1_estimated_watts = (
            status["pv1_voltage"]
            * status["pv1_current"]
        )

        pv2_estimated_watts = (
            status["pv2_voltage"]
            * status["pv2_current"]
        )

        estimated_pv_watts = (
            pv1_estimated_watts
            + pv2_estimated_watts
        )

        pv_current = (
            status["pv1_current"]
            + status["pv2_current"]
        )

        active_pv_voltages = []

        if status["pv1_current"] > 0:
            active_pv_voltages.append(
                status["pv1_voltage"]
            )

        if status["pv2_current"] > 0:
            active_pv_voltages.append(
                status["pv2_voltage"]
            )

        if active_pv_voltages:
            pv_voltage = (
                sum(active_pv_voltages)
                / len(active_pv_voltages)
            )
        else:
            pv_voltage = max(
                status["pv1_voltage"],
                status["pv2_voltage"],
            )

        if inverter_index == 0:
            pv_watts = direct_pv_watts
            pv_quality = "measured"
        else:
            pv_watts = estimated_pv_watts
            pv_quality = "estimated"

        readings.append(
            {
                "inverter_id": inverter_id,
                "grid_voltage": status["grid_voltage"],
                "grid_frequency": status["grid_frequency"],
                "output_voltage": status["output_voltage"],
                "output_frequency": status["output_frequency"],
                "output_va": status["output_va"],
                "output_watts": status["output_watts"],
                "load_percent": status["load_percent"],
                "battery_voltage": status["battery_voltage"],
                "battery_charge_amps": (
                    status["battery_charge_current"]
                ),
                "battery_discharge_amps": (
                    status["battery_discharge_current"]
                ),
                "battery_soc": status["battery_soc"],
                "inverter_temperature_c": None,
                "pv_voltage": pv_voltage,
                "pv_current": pv_current,
                "pv_watts": pv_watts,
                "reported_total_output_watts": (
                    status["total_output_watts"]
                ),
                "status": (
                    f"eg4_serial_parallel:"
                    f"{status['mode']}:"
                    f"pv_{pv_quality}"
                ),
            }
        )

    return readings


def build_system_sample(inverters):
    total_pv = sum(
        i["pv_watts"]
        for i in inverters
    )

    # Each QPGS response contains the parallel system's
    # combined output. Because QPGS0/QPGS1 are queried
    # sequentially, average their reported totals.
    reported_loads = [
        i["reported_total_output_watts"]
        for i in inverters
        if i.get("reported_total_output_watts") is not None
    ]

    if reported_loads:
        total_load = (
            sum(reported_loads)
            / len(reported_loads)
        )
    else:
        total_load = sum(
            i["output_watts"]
            for i in inverters
        )

    battery_voltage = (
        sum(
            i["battery_voltage"]
            for i in inverters
        )
        / len(inverters)
    )

    battery_soc = (
        sum(
            i["battery_soc"]
            for i in inverters
        )
        / len(inverters)
    )

    total_charge_watts = sum(
        i["battery_charge_amps"]
        * i["battery_voltage"]
        for i in inverters
    )

    total_discharge_watts = sum(
        i["battery_discharge_amps"]
        * i["battery_voltage"]
        for i in inverters
    )

    # We can assert zero grid power when neither inverter
    # sees AC input. When grid is present, QPGS does not
    # provide a trustworthy grid-watt value.
    if all(
        i["grid_voltage"] < 1.0
        for i in inverters
    ):
        grid_watts = 0.0
    else:
        grid_watts = 0.0

    return {
        "total_pv_watts": total_pv,
        "total_load_watts": total_load,
        "battery_voltage": battery_voltage,
        "battery_soc": battery_soc,
        "battery_watts": (
            total_charge_watts
            - total_discharge_watts
        ),
        "grid_watts": grid_watts,
        "inverter_count": len(inverters),
        "source": "eg4_serial_parallel",
    }

def insert_inverter_samples(
    conn,
    timestamp,
    inverters,
):
    conn.executemany(
        """
        INSERT INTO inverter_samples (
            timestamp,
            inverter_id,
            grid_voltage,
            grid_frequency,
            output_voltage,
            output_frequency,
            output_va,
            output_watts,
            load_percent,
            battery_voltage,
            battery_charge_amps,
            battery_discharge_amps,
            battery_soc,
            inverter_temperature_c,
            pv_voltage,
            pv_current,
            pv_watts,
            status
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        [
            (
                timestamp,
                i["inverter_id"],
                round(i["grid_voltage"], 2),
                round(i["grid_frequency"], 2),
                round(i["output_voltage"], 2),
                round(i["output_frequency"], 2),
                round(i["output_va"], 1),
                round(i["output_watts"], 1),
                round(i["load_percent"], 1),
                round(i["battery_voltage"], 2),
                round(i["battery_charge_amps"], 2),
                round(i["battery_discharge_amps"], 2),
                round(i["battery_soc"], 1),
                (
                    round(
                        i["inverter_temperature_c"],
                        1,
                    )
                    if i["inverter_temperature_c"] is not None
                    else None
                ),
                round(i["pv_voltage"], 1),
                round(i["pv_current"], 2),
                round(i["pv_watts"], 1),
                i["status"],
            )
            for i in inverters
        ],
    )


def insert_system_sample(
    conn,
    timestamp,
    sample,
):
    conn.execute(
        """
        INSERT INTO system_samples (
            timestamp,
            total_pv_watts,
            total_load_watts,
            battery_voltage,
            battery_soc,
            battery_watts,
            grid_watts,
            inverter_count,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            round(
                sample["total_pv_watts"],
                1,
            ),
            round(
                sample["total_load_watts"],
                1,
            ),
            round(
                sample["battery_voltage"],
                2,
            ),
            round(
                sample["battery_soc"],
                1,
            ),
            round(
                sample["battery_watts"],
                1,
            ),
            round(
                sample["grid_watts"],
                1,
            ),
            sample["inverter_count"],
            sample["source"],
        ),
    )


def write_live_sample(
    timestamp,
    sample,
):
    live_data = {
        "timestamp": timestamp,
        "pv_watts": round(
            sample["total_pv_watts"],
            1,
        ),
        "load_watts": round(
            sample["total_load_watts"],
            1,
        ),
        "battery_voltage": round(
            sample["battery_voltage"],
            2,
        ),
        "battery_soc": round(
            sample["battery_soc"],
            1,
        ),
        "battery_watts": round(
            sample["battery_watts"],
            1,
        ),
        "grid_watts": round(
            sample["grid_watts"],
            1,
        ),
        "inverter_count": (
            sample["inverter_count"]
        ),
        "source": sample["source"],
    }

    temp_path = LIVE_PATH.with_suffix(
        ".tmp"
    )

    with temp_path.open("w") as file:
        json.dump(live_data, file)

    os.replace(
        temp_path,
        LIVE_PATH,
    )


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist. "
            "Run init_db.py first."
        )

    conn = connect_db()

    print(
        "EG4 solar collector v2 started."
    )
    print("Database:", DB_PATH)
    print("Mode: EG4 SERIAL PARALLEL")
    print(
        f"Sampling every "
        f"{SAMPLE_INTERVAL} seconds."
    )

    try:
        while True:
            timestamp = (
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )

            try:
                inverter_readings = (
                    get_real_inverter_readings()
                )
            except Exception as exc:
                print(
                    f"EG4 parallel read failed: {exc}",
                    flush=True,
                )
                time.sleep(SAMPLE_INTERVAL)
                continue

            system_sample = (
                build_system_sample(
                    inverter_readings
                )
            )

            try:
                # Keep the two inverter rows and the system row
                # atomic, but release SQLite's writer lock
                # immediately after this telemetry sample.
                with conn:
                    insert_inverter_samples(
                        conn,
                        timestamp,
                        inverter_readings,
                    )

                    insert_system_sample(
                        conn,
                        timestamp,
                        system_sample,
                    )

            except sqlite3.OperationalError as exc:
                print(
                    f"Database write failed: {exc}",
                    flush=True,
                )
                time.sleep(SAMPLE_INTERVAL)
                continue

            # Publish live state only after the database
            # transaction committed successfully.
            write_live_sample(
                timestamp,
                system_sample,
            )

            print(
                f"{timestamp} | "
                f"PV "
                f"{system_sample['total_pv_watts']:7.1f} W | "
                f"Load "
                f"{system_sample['total_load_watts']:7.1f} W | "
                f"Battery "
                f"{system_sample['battery_soc']:5.1f}% "
                f"{system_sample['battery_voltage']:5.2f} V | "
                f"Batt Power "
                f"{system_sample['battery_watts']:7.1f} W"
            )

            time.sleep(
                SAMPLE_INTERVAL
            )

    except KeyboardInterrupt:
        print(
            "\nStopping collector..."
        )

    finally:
        conn.close()
        print(
            "Database connection closed."
        )


if __name__ == "__main__":
    main()
