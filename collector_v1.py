import random
import sqlite3
import time
from datetime import datetime

DB_PATH = "solar.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pv_watts REAL NOT NULL,
            load_watts REAL NOT NULL,
            battery_voltage REAL NOT NULL,
            battery_soc REAL NOT NULL,
            battery_watts REAL NOT NULL,
            grid_watts REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def get_mock_reading():
    pv_watts = random.uniform(2500, 6500)
    load_watts = random.uniform(800, 3000)
    battery_voltage = random.uniform(50.5, 53.5)
    battery_soc = random.uniform(60, 95)

    surplus = pv_watts - load_watts

    if surplus >= 0:
        battery_watts = min(surplus, 4000)
        grid_watts = max(0, surplus - battery_watts)
    else:
        battery_watts = max(surplus, -4000)
        grid_watts = max(0, load_watts - pv_watts - abs(battery_watts))

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "pv_watts": round(pv_watts, 1),
        "load_watts": round(load_watts, 1),
        "battery_voltage": round(battery_voltage, 2),
        "battery_soc": round(battery_soc, 1),
        "battery_watts": round(battery_watts, 1),
        "grid_watts": round(grid_watts, 1),
    }


def save_reading(reading):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO readings (
            timestamp,
            pv_watts,
            load_watts,
            battery_voltage,
            battery_soc,
            battery_watts,
            grid_watts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        reading["timestamp"],
        reading["pv_watts"],
        reading["load_watts"],
        reading["battery_voltage"],
        reading["battery_soc"],
        reading["battery_watts"],
        reading["grid_watts"],
    ))

    conn.commit()
    conn.close()


def main():
    init_db()

    print("Solar monitor collector started.")
    print("Mode: MOCK")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            reading = get_mock_reading()
            save_reading(reading)

            print(
                f'{reading["timestamp"]} | '
                f'PV {reading["pv_watts"]:7.1f} W | '
                f'Load {reading["load_watts"]:7.1f} W | '
                f'Battery {reading["battery_soc"]:5.1f}% '
                f'{reading["battery_voltage"]:5.2f} V | '
                f'Batt Power {reading["battery_watts"]:7.1f} W | '
                f'Grid {reading["grid_watts"]:7.1f} W'
            )

            time.sleep(5)

    except KeyboardInterrupt:
        print("\nCollector stopped.")


if __name__ == "__main__":
    main()
