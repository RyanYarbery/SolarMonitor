"""Read or set plant configuration stored in the system_config table.

    python configure.py                      # list
    python configure.py installed_pv_kw 11.2 # set

Operational settings (paths, serial port, retention) are environment
variables; see settings.py and README.
"""

import sqlite3
import sys

import settings

KEYS = {
    "installed_pv_kw": "Installed PV array size in kW (enables specific yield and capacity factor)",
    "battery_capacity_kwh": "Usable battery capacity in kWh (enables equivalent cycle counts)",
    "production_threshold_watts": "PV watts above which the day counts as producing",
}


def main(argv):
    conn = sqlite3.connect(settings.DB_PATH)
    try:
        if len(argv) == 0:
            rows = dict(conn.execute("SELECT key, value FROM system_config").fetchall())
            for key, help_text in KEYS.items():
                print(f"{key:28s} = {rows.get(key, '<unset>'):>10s}   {help_text}")
            print(f"{'schema_version':28s} = {rows.get('schema_version', '?'):>10s}")
            return 0
        if len(argv) != 2 or argv[0] not in KEYS:
            print(__doc__)
            print("Known keys:", ", ".join(KEYS))
            return 2
        key, value = argv
        float(value)  # validate numeric
        with conn:
            conn.execute(
                """
                INSERT INTO system_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        print(f"{key} = {value}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
