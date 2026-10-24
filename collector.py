"""Poll the EG4 inverter pair and record telemetry.

Each cycle writes one row per inverter plus one combined system row in a
single short transaction, publishes a live JSON snapshot, and records
mode/fault transitions in the events table.
"""

import json
import os
import sqlite3
import time
import traceback

import alerts
import sdnotify
import settings
from eg4 import EG4Serial
from timeutil import now_utc, to_iso

# Consecutive read failures before a telemetry_lost event is recorded.
FAILURE_EVENT_THRESHOLD = 6

NO_FAULT = ("", "00", "0")


def connect_db():
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


# ------------------------------------------------------------------- readings


def read_inverters(link):
    """Return one reading dict per inverter from the parallel queries.

    QPGS0/QPGS1 describe both units. The serial cable is attached to QPGS
    index 0, so QPIGS/QPIGS2 give that unit's directly measured PV charging
    power; the other unit's PV is estimated from voltage * current.
    """
    statuses = link.get_all_parallel_status()
    direct_status = link.get_status()
    direct_pv2 = link.get_pv2_status()
    direct_pv_watts = direct_status["pv_charging_power"] + direct_pv2["pv2_watts"]

    readings = []
    for index, status in enumerate(statuses):
        pv1_w = status["pv1_voltage"] * status["pv1_current"]
        pv2_w = status["pv2_voltage"] * status["pv2_current"]

        active_voltages = [
            v
            for v, i in (
                (status["pv1_voltage"], status["pv1_current"]),
                (status["pv2_voltage"], status["pv2_current"]),
            )
            if i > 0
        ]
        if active_voltages:
            pv_voltage = sum(active_voltages) / len(active_voltages)
        else:
            pv_voltage = max(status["pv1_voltage"], status["pv2_voltage"])

        if index == 0:
            pv1_watts = direct_status["pv_charging_power"]
            pv2_watts = direct_pv2["pv2_watts"]
            pv_watts, pv_quality = direct_pv_watts, "measured"
            temperature = direct_status["temperature_c"]
        else:
            pv1_watts, pv2_watts = pv1_w, pv2_w
            pv_watts, pv_quality = pv1_w + pv2_w, "estimated"
            temperature = None

        readings.append(
            {
                "inverter_id": index + 1,
                "grid_voltage": status["grid_voltage"],
                "grid_frequency": status["grid_frequency"],
                "output_voltage": status["output_voltage"],
                "output_frequency": status["output_frequency"],
                "output_va": status["output_va"],
                "output_watts": status["output_watts"],
                "load_percent": status["load_percent"],
                "battery_voltage": status["battery_voltage"],
                "battery_charge_amps": status["battery_charge_current"],
                "battery_discharge_amps": status["battery_discharge_current"],
                "battery_soc": status["battery_soc"],
                "inverter_temperature_c": temperature,
                "pv_voltage": pv_voltage,
                "pv_current": status["pv1_current"] + status["pv2_current"],
                "pv_watts": pv_watts,
                "pv1_voltage": status["pv1_voltage"],
                "pv1_current": status["pv1_current"],
                "pv1_watts": pv1_watts,
                "pv2_voltage": status["pv2_voltage"],
                "pv2_current": status["pv2_current"],
                "pv2_watts": pv2_watts,
                "pv_quality": pv_quality,
                "mode": status["mode"],
                "fault_code": status["fault_code"],
                "reported_total_output_watts": status["total_output_watts"],
                "status": f"eg4_serial_parallel:{status['mode']}:pv_{pv_quality}",
            }
        )
    return readings


def build_system_sample(inverters):
    total_pv = sum(i["pv_watts"] for i in inverters)

    # Each QPGS response carries the parallel system's combined output.
    # The two are queried sequentially, so average them.
    reported = [
        i["reported_total_output_watts"]
        for i in inverters
        if i.get("reported_total_output_watts") is not None
    ]
    if reported:
        total_load = sum(reported) / len(reported)
    else:
        total_load = sum(i["output_watts"] for i in inverters)

    n = len(inverters)
    battery_voltage = sum(i["battery_voltage"] for i in inverters) / n
    battery_soc = sum(i["battery_soc"] for i in inverters) / n
    charge_w = sum(i["battery_charge_amps"] * i["battery_voltage"] for i in inverters)
    discharge_w = sum(i["battery_discharge_amps"] * i["battery_voltage"] for i in inverters)

    grid_voltage = max(i["grid_voltage"] for i in inverters)

    return {
        "total_pv_watts": total_pv,
        "total_load_watts": total_load,
        "grid_voltage": grid_voltage,
        "battery_voltage": battery_voltage,
        "battery_soc": battery_soc,
        "battery_watts": charge_w - discharge_w,
        # QPGS exposes no trustworthy grid power figure. Stored as NULL
        # (unknown) rather than a misleading zero.
        "grid_watts": None,
        "inverter_count": n,
        "source": "eg4_serial_parallel",
        "any_fault": any(i["fault_code"] not in NO_FAULT for i in inverters),
        "modes": {i["inverter_id"]: i["mode"] for i in inverters},
    }


# -------------------------------------------------------------------- storage


def _r(value, digits):
    return None if value is None else round(value, digits)


def insert_inverter_samples(conn, timestamp, inverters):
    conn.executemany(
        """
        INSERT INTO inverter_samples (
            timestamp, inverter_id,
            grid_voltage, grid_frequency,
            output_voltage, output_frequency, output_va, output_watts,
            load_percent,
            battery_voltage, battery_charge_amps, battery_discharge_amps,
            battery_soc, inverter_temperature_c,
            pv_voltage, pv_current, pv_watts,
            pv1_voltage, pv1_current, pv1_watts,
            pv2_voltage, pv2_current, pv2_watts,
            mode, fault_code, status
        ) VALUES (?,?, ?,?, ?,?,?,?, ?, ?,?,?, ?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?)
        """,
        [
            (
                timestamp,
                i["inverter_id"],
                _r(i["grid_voltage"], 2),
                _r(i["grid_frequency"], 2),
                _r(i["output_voltage"], 2),
                _r(i["output_frequency"], 2),
                _r(i["output_va"], 1),
                _r(i["output_watts"], 1),
                _r(i["load_percent"], 1),
                _r(i["battery_voltage"], 2),
                _r(i["battery_charge_amps"], 2),
                _r(i["battery_discharge_amps"], 2),
                _r(i["battery_soc"], 1),
                _r(i["inverter_temperature_c"], 1),
                _r(i["pv_voltage"], 1),
                _r(i["pv_current"], 2),
                _r(i["pv_watts"], 1),
                _r(i["pv1_voltage"], 1),
                _r(i["pv1_current"], 2),
                _r(i["pv1_watts"], 1),
                _r(i["pv2_voltage"], 1),
                _r(i["pv2_current"], 2),
                _r(i["pv2_watts"], 1),
                i["mode"],
                i["fault_code"],
                i["status"],
            )
            for i in inverters
        ],
    )


def insert_system_sample(conn, timestamp, sample):
    conn.execute(
        """
        INSERT INTO system_samples (
            timestamp, total_pv_watts, total_load_watts,
            battery_voltage, battery_soc, battery_watts,
            grid_watts, inverter_count, source
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            timestamp,
            _r(sample["total_pv_watts"], 1),
            _r(sample["total_load_watts"], 1),
            _r(sample["battery_voltage"], 2),
            _r(sample["battery_soc"], 1),
            _r(sample["battery_watts"], 1),
            _r(sample["grid_watts"], 1),
            sample["inverter_count"],
            sample["source"],
        ),
    )


def record_event(conn, timestamp, event_type, message, severity="info", inverter_id=None):
    conn.execute(
        """
        INSERT INTO events (timestamp, event_type, severity, source, inverter_id, message)
        VALUES (?, ?, ?, 'collector', ?, ?)
        """,
        (timestamp, event_type, severity, inverter_id, message),
    )


class StateTracker:
    """Detects per-inverter mode and fault transitions between cycles."""

    def __init__(self):
        self.last = {}  # inverter_id -> (mode, fault_code)

    def events_for(self, inverters):
        events = []
        for i in inverters:
            key = i["inverter_id"]
            current = (i["mode"], i["fault_code"])
            previous = self.last.get(key)
            if previous is None:
                self.last[key] = current
                continue
            if current == previous:
                continue
            old_mode, old_fault = previous
            mode, fault = current
            if mode != old_mode:
                events.append(
                    (
                        "mode_change",
                        f"Inverter {key} mode {old_mode} -> {mode}",
                        "warning" if mode == "F" else "info",
                        key,
                    )
                )
            if fault != old_fault:
                if fault in NO_FAULT:
                    events.append(
                        ("fault_cleared", f"Inverter {key} fault {old_fault} cleared", "info", key)
                    )
                else:
                    events.append(("fault", f"Inverter {key} fault code {fault}", "error", key))
            self.last[key] = current
        return events


class LowSocTracker:
    """Fires once when SOC drops below the threshold, clears with hysteresis."""

    def __init__(self, threshold, clear_margin=settings.LOW_SOC_CLEAR_MARGIN):
        self.threshold = threshold
        self.clear_margin = clear_margin
        self.low = False

    def update(self, soc):
        """Return 'low', 'recovered' or None."""
        if soc is None:
            return None
        if not self.low and soc < self.threshold:
            self.low = True
            return "low"
        if self.low and soc >= self.threshold + self.clear_margin:
            self.low = False
            return "recovered"
        return None


class GridTracker:
    """Detects utility/generator AC appearing at or disappearing from the input.

    Hysteresis: present at or above GRID_ON_VOLTS, absent below GRID_OFF_VOLTS.
    The first cycle only records the initial state and emits nothing.
    """

    def __init__(self, on_volts=settings.GRID_ON_VOLTS, off_volts=settings.GRID_OFF_VOLTS):
        self.on_volts = on_volts
        self.off_volts = off_volts
        self.present = None

    def update(self, grid_voltage):
        """Return 'on', 'off' or None."""
        if grid_voltage is None:
            return None
        if self.present is None:
            self.present = grid_voltage >= self.on_volts
            return None
        if not self.present and grid_voltage >= self.on_volts:
            self.present = True
            return "on"
        if self.present and grid_voltage < self.off_volts:
            self.present = False
            return "off"
        return None


def load_plant_config(conn):
    return dict(conn.execute("SELECT key, value FROM system_config").fetchall())


def dispatch_alert(conn, event_type, message, severity, inverter_id=None):
    """Map a recorded event onto an ntfy alert with cooldown. Never raises."""
    try:
        if event_type == "fault":
            alerts.raise_alert(
                conn,
                f"fault:{inverter_id}",
                f"Inverter {inverter_id} fault",
                message,
                "error",
                ("rotating_light",),
            )
        elif event_type == "fault_cleared":
            alerts.clear_alert(
                conn, f"fault:{inverter_id}", f"Inverter {inverter_id} fault cleared", message
            )
        elif event_type == "mode_change" and severity == "warning":
            alerts.raise_alert(
                conn, f"mode_fault:{inverter_id}", "Inverter entered fault mode", message, "error"
            )
        elif event_type == "telemetry_lost":
            alerts.raise_alert(
                conn, "telemetry_lost", "Inverter telemetry lost", message, "warning", ("warning",)
            )
        elif event_type == "telemetry_restored":
            alerts.clear_alert(conn, "telemetry_lost", "Inverter telemetry restored", message)
        elif event_type == "grid_on":
            alerts.raise_alert(
                conn, "grid_present", "Utility power on", message, "warning", ("electric_plug",)
            )
        elif event_type == "grid_off":
            alerts.clear_alert(
                conn, "grid_present", "Utility power off", message, ("electric_plug",)
            )
        elif event_type == "low_soc":
            alerts.raise_alert(conn, "low_soc", "Battery low", message, "warning", ("battery",))
        elif event_type == "soc_recovered":
            alerts.clear_alert(conn, "low_soc", "Battery recovered", message)
    except Exception as exc:  # alerting must never take the collector down
        print(f"Alert dispatch failed: {exc}", flush=True)


# ------------------------------------------------------------------ live file


class LiveWriter:
    def __init__(self, path=settings.LIVE_PATH):
        self.path = path
        self.enabled = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.enabled = False
            print(
                f"Live snapshot disabled: cannot create {self.path.parent} ({exc}). "
                "Add RuntimeDirectory=solar-monitor to the systemd unit.",
                flush=True,
            )

    def write(self, timestamp, sample, inverters):
        if not self.enabled:
            return
        data = {
            "timestamp": timestamp,
            "pv_watts": _r(sample["total_pv_watts"], 1),
            "load_watts": _r(sample["total_load_watts"], 1),
            "battery_voltage": _r(sample["battery_voltage"], 2),
            "battery_soc": _r(sample["battery_soc"], 1),
            "battery_watts": _r(sample["battery_watts"], 1),
            "grid_watts": _r(sample["grid_watts"], 1),
            "grid_voltage": _r(sample["grid_voltage"], 1),
            "grid_present": sample.get("grid_present"),
            "inverter_count": sample["inverter_count"],
            "source": sample["source"],
            "any_fault": sample["any_fault"],
            "inverters": [
                {
                    "inverter_id": i["inverter_id"],
                    "mode": i["mode"],
                    "fault_code": i["fault_code"],
                    "pv_watts": _r(i["pv_watts"], 1),
                    "pv1_watts": _r(i["pv1_watts"], 1),
                    "pv2_watts": _r(i["pv2_watts"], 1),
                    "pv_quality": i["pv_quality"],
                    "output_watts": _r(i["output_watts"], 1),
                }
                for i in inverters
            ],
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"Live snapshot write failed: {exc}", flush=True)


# ----------------------------------------------------------------------- main


def main():
    if not settings.DB_PATH.exists():
        raise FileNotFoundError(f"{settings.DB_PATH} does not exist. Run init_db.py first.")

    conn = connect_db()
    link = EG4Serial()
    live = LiveWriter()
    tracker = StateTracker()
    plant = load_plant_config(conn)
    soc_tracker = LowSocTracker(float(plant.get("low_soc_alert_percent") or 20))
    grid_tracker = GridTracker()
    failures = 0
    lost_reported = False
    cycles = 0

    print("EG4 solar collector v3 started.")
    print("Database:", settings.DB_PATH)
    print("Serial:", link.port, "@", link.baud)
    print(f"Sampling every {settings.SAMPLE_INTERVAL} seconds.")
    print("Alerts:", settings.NTFY_URL or "disabled", flush=True)
    sdnotify.ready()

    try:
        while True:
            cycle_started = time.monotonic()
            timestamp = to_iso(now_utc())
            sdnotify.watchdog()
            cycles += 1
            if cycles % 60 == 0:  # pick up configure.py changes every ~10 minutes
                try:
                    plant = load_plant_config(conn)
                    soc_tracker.threshold = float(plant.get("low_soc_alert_percent") or 20)
                except sqlite3.Error:
                    pass

            try:
                inverters = read_inverters(link)
            except Exception as exc:
                failures += 1
                print(f"EG4 read failed ({failures}): {exc}", flush=True)
                if failures >= FAILURE_EVENT_THRESHOLD and not lost_reported:
                    try:
                        with conn:
                            record_event(
                                conn,
                                timestamp,
                                "telemetry_lost",
                                f"{failures} consecutive read failures: {exc}",
                                "warning",
                            )
                        lost_reported = True
                    except sqlite3.Error as db_exc:
                        print(f"Event write failed: {db_exc}", flush=True)
                _sleep_remaining(cycle_started)
                continue

            if lost_reported:
                lost_reported = False
                recovered = failures
                failures = 0
                pending_events = [
                    (
                        "telemetry_restored",
                        f"Telemetry restored after {recovered} failures",
                        "info",
                        None,
                    )
                ]
            else:
                failures = 0
                pending_events = []

            system = build_system_sample(inverters)
            pending_events.extend(tracker.events_for(inverters))

            grid_change = grid_tracker.update(system["grid_voltage"])
            if grid_change == "on":
                pending_events.append(
                    (
                        "grid_on",
                        f"AC input detected at {system['grid_voltage']:.0f} V",
                        "warning",
                        None,
                    )
                )
            elif grid_change == "off":
                pending_events.append(("grid_off", "AC input lost", "info", None))
            system["grid_present"] = grid_tracker.present

            soc_change = soc_tracker.update(system["battery_soc"])
            if soc_change == "low":
                pending_events.append(
                    (
                        "low_soc",
                        f"Battery SOC {system['battery_soc']:.0f}% is below {soc_tracker.threshold:.0f}%",
                        "warning",
                        None,
                    )
                )
            elif soc_change == "recovered":
                pending_events.append(
                    (
                        "soc_recovered",
                        f"Battery SOC back to {system['battery_soc']:.0f}%",
                        "info",
                        None,
                    )
                )

            try:
                with conn:
                    insert_inverter_samples(conn, timestamp, inverters)
                    insert_system_sample(conn, timestamp, system)
                    for event_type, message, severity, inverter_id in pending_events:
                        record_event(conn, timestamp, event_type, message, severity, inverter_id)
                        dispatch_alert(conn, event_type, message, severity, inverter_id)
                        print(f"EVENT {severity}: {message}", flush=True)
            except sqlite3.Error as exc:
                print(f"Database write failed: {exc}", flush=True)
                _sleep_remaining(cycle_started)
                continue

            live.write(timestamp, system, inverters)
            sdnotify.status(
                f"PV {system['total_pv_watts']:.0f} W, load {system['total_load_watts']:.0f} W, "
                f"SOC {system['battery_soc']:.0f}%"
            )

            print(
                f"{timestamp} | PV {system['total_pv_watts']:7.1f} W | "
                f"Load {system['total_load_watts']:7.1f} W | "
                f"Battery {system['battery_soc']:5.1f}% {system['battery_voltage']:5.2f} V | "
                f"Batt Power {system['battery_watts']:7.1f} W | "
                f"Modes {'/'.join(system['modes'].values())}",
                flush=True,
            )
            _sleep_remaining(cycle_started)

    except KeyboardInterrupt:
        print("\nStopping collector...")
    except Exception:
        traceback.print_exc()
        raise
    finally:
        link.close()
        conn.close()
        print("Database connection closed.")


def _sleep_remaining(cycle_started):
    """Keep cycles on a fixed cadence regardless of serial latency."""
    elapsed = time.monotonic() - cycle_started
    time.sleep(max(0.5, settings.SAMPLE_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
