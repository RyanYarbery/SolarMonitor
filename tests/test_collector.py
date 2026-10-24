import unittest

import collector
from tests.helpers import ROOT  # noqa: F401


def inv(inverter_id, mode="B", fault="00", **kw):
    base = {
        "inverter_id": inverter_id,
        "pv_watts": 1000.0,
        "output_watts": 400.0,
        "battery_voltage": 52.0,
        "battery_soc": 80.0,
        "battery_charge_amps": 10.0,
        "battery_discharge_amps": 0.0,
        "reported_total_output_watts": 800.0,
        "mode": mode,
        "fault_code": fault,
    }
    base.update(kw)
    return base


class SystemSampleTests(unittest.TestCase):
    def test_totals(self):
        s = collector.build_system_sample(
            [inv(1), inv(2, battery_charge_amps=0, battery_discharge_amps=5)]
        )
        self.assertEqual(s["total_pv_watts"], 2000.0)
        self.assertEqual(s["total_load_watts"], 800.0)  # averaged reported totals
        self.assertAlmostEqual(s["battery_watts"], 10 * 52 - 5 * 52)
        self.assertIsNone(s["grid_watts"])
        self.assertFalse(s["any_fault"])

    def test_fault_flag(self):
        self.assertTrue(collector.build_system_sample([inv(1), inv(2, fault="07")])["any_fault"])


class StateTrackerTests(unittest.TestCase):
    def test_first_cycle_emits_nothing(self):
        t = collector.StateTracker()
        self.assertEqual(t.events_for([inv(1), inv(2)]), [])

    def test_mode_change_and_fault(self):
        t = collector.StateTracker()
        t.events_for([inv(1), inv(2)])
        events = t.events_for([inv(1, mode="F", fault="02"), inv(2)])
        types = sorted(e[0] for e in events)
        self.assertEqual(types, ["fault", "mode_change"])
        self.assertTrue(all(e[3] == 1 for e in events))
        # back to normal
        events = t.events_for([inv(1), inv(2)])
        self.assertEqual(sorted(e[0] for e in events), ["fault_cleared", "mode_change"])
        # steady state
        self.assertEqual(t.events_for([inv(1), inv(2)]), [])


if __name__ == "__main__":
    unittest.main()


class StubLink:
    """Answers the collector's queries from canned frames; no hardware."""

    def __init__(self):
        import eg4
        from tests.test_eg4 import QPGS0, QPIGS, QPIGS2

        self.parallel = [eg4.parse_qpgs(QPGS0, 0), dict(eg4.parse_qpgs(QPGS0, 1), parallel_id=2)]
        self.status = eg4.parse_qpigs(QPIGS)
        self.pv2 = eg4.parse_qpigs2(QPIGS2)

    def get_all_parallel_status(self):
        return self.parallel

    def get_status(self):
        return self.status

    def get_pv2_status(self):
        return self.pv2


class CollectorCycleTests(unittest.TestCase):
    def setUp(self):
        from tests.helpers import fresh_db

        self.conn, self.path = fresh_db()

    def tearDown(self):
        from tests.helpers import cleanup_db

        self.conn.close()
        cleanup_db(self.path)

    def test_one_cycle_writes_rows_and_live_file(self):
        import json
        import tempfile
        from pathlib import Path

        import settings
        from timeutil import now_utc, to_iso

        live_path = Path(tempfile.mkdtemp()) / "run" / "latest.json"
        readings = collector.read_inverters(StubLink())
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["pv_quality"], "measured")
        self.assertEqual(readings[0]["pv_watts"], 412 + 385)  # QPIGS + QPIGS2
        self.assertEqual(readings[1]["pv_quality"], "estimated")
        self.assertEqual(readings[0]["inverter_temperature_c"], 35)

        system = collector.build_system_sample(readings)
        ts = to_iso(now_utc())
        conn = collector.connect_db()
        try:
            with conn:
                collector.insert_inverter_samples(conn, ts, readings)
                collector.insert_system_sample(conn, ts, system)
                collector.record_event(conn, ts, "test", "hello", "info", 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inverter_samples").fetchone()[0], 2)
            row = conn.execute(
                "SELECT mode, fault_code, status FROM inverter_samples WHERE inverter_id=1"
            ).fetchone()
            self.assertEqual(tuple(row), ("B", "00", "eg4_serial_parallel:B:pv_measured"))
            self.assertEqual(
                conn.execute("SELECT grid_watts FROM system_samples").fetchone()[0], None
            )
        finally:
            conn.close()

        writer = collector.LiveWriter(live_path)  # parent dir is created on demand
        writer.write(ts, system, readings)
        data = json.loads(live_path.read_text())
        self.assertEqual(data["timestamp"], ts)
        self.assertEqual(data["inverters"][1]["pv_quality"], "estimated")
        self.assertFalse(data["any_fault"])
        settings.LIVE_PATH = settings.LIVE_PATH  # untouched
