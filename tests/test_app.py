import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import app as webapp
import settings
from tests.helpers import cleanup_db, fresh_db
from timeutil import now_utc, to_iso


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        self.live = Path(tempfile.mktemp(suffix=".json"))
        settings.LIVE_PATH = self.live
        now = now_utc()
        rows = []
        t = now - timedelta(minutes=50)
        while t < now:
            rows.append((to_iso(t), 800.0, 400.0, 52.0, 70.0, 50.0, None, 2, "test"))
            t += timedelta(seconds=10)
        self.conn.executemany(
            "INSERT INTO system_samples(timestamp,total_pv_watts,total_load_watts,battery_voltage,"
            "battery_soc,battery_watts,grid_watts,inverter_count,source) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.execute(
            "INSERT INTO inverter_samples(timestamp,inverter_id,pv_watts,mode,fault_code,status)"
            " VALUES(?,1,400,'B','00','eg4_serial_parallel:B:pv_measured')",
            (rows[-1][0],),
        )
        self.conn.execute(
            "INSERT INTO events(timestamp,event_type,severity,source,message)"
            " VALUES(?,'mode_change','info','collector','Inverter 1 mode L -> B')",
            (rows[-1][0],),
        )
        self.conn.commit()
        self.client = webapp.app.test_client()

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)
        if self.live.exists():
            self.live.unlink()

    def test_latest_from_db_is_fresh(self):
        r = self.client.get("/api/latest")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertFalse(d["stale"])
        self.assertLess(d["age_seconds"], 60)

    def test_latest_from_stale_live_file(self):
        old = to_iso(now_utc() - timedelta(hours=2))
        self.live.write_text(json.dumps({"timestamp": old, "pv_watts": 1}))
        d = self.client.get("/api/latest").get_json()
        self.assertTrue(d["stale"])
        self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_history_raw_hour_buckets_with_offset_timestamps(self):
        d = self.client.get("/api/history?hours=1").get_json()
        self.assertEqual(d["source"], "raw")
        self.assertGreater(len(d["points"]), 100)
        self.assertTrue(all(not p["gap_before"] for p in d["points"][1:]))
        self.assertAlmostEqual(d["points"][0]["pv_value"], 800.0)

    def test_inverters_have_mode_name_and_quality(self):
        d = self.client.get("/api/inverters").get_json()
        self.assertEqual(d[0]["mode_name"], "battery")
        self.assertEqual(d[0]["pv_quality"], "measured")

    def test_events(self):
        d = self.client.get("/api/events").get_json()
        self.assertEqual(d[0]["event_type"], "mode_change")


if __name__ == "__main__":
    unittest.main()


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        settings.LIVE_PATH = Path(tempfile.mktemp(suffix=".json"))
        self.client = webapp.app.test_client()
        from timeutil import local_day_bounds, today_local

        self.today = today_local()
        self.start, _ = local_day_bounds(self.today)

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def test_energy_today_combines_quarters_and_raw_tail(self):
        q = self.start
        self.conn.execute(
            "INSERT INTO quarter_hour_summary(timestamp,pv_energy_wh,load_energy_wh,sample_count)"
            " VALUES(?,250,100,90)",
            (to_iso(q),),
        )
        # raw tail after the quarter: 1000 W for 60 s = 16.67 Wh
        t = q + timedelta(minutes=15)
        for i in range(7):
            self.conn.execute(
                "INSERT INTO system_samples(timestamp,total_pv_watts,total_load_watts,battery_soc,battery_watts)"
                " VALUES(?,1000,500,60,-1000)",
                (to_iso(t + timedelta(seconds=10 * i)),),
            )
        self.conn.execute("UPDATE system_config SET value='10' WHERE key='battery_capacity_kwh'")
        self.conn.execute(
            "INSERT INTO daily_summary(date,pv_energy_wh,load_energy_wh,battery_soc_min) VALUES(?,12000,8000,45)",
            ((self.today - timedelta(days=1)).isoformat(),),
        )
        self.conn.commit()
        s = self.client.get("/api/summary").get_json()
        self.assertEqual(s["today"]["pv_wh"], 267)
        self.assertEqual(s["today"]["load_wh"], 108)
        self.assertEqual(s["today"]["battery_soc_min"], 60)
        self.assertEqual(s["yesterday"]["pv_energy_wh"], 12000)
        # discharging 1000 W at 60% of 10 kWh -> 6 h
        self.assertEqual(s["battery"], {"state": "discharging", "hours": 6.0})

    def test_runtime_states(self):
        self.assertIsNone(webapp.battery_runtime({"battery_soc": 50, "battery_watts": 100}, 0))
        self.assertEqual(
            webapp.battery_runtime({"battery_soc": 50, "battery_watts": 10}, 10)["state"], "idle"
        )
        charging = webapp.battery_runtime({"battery_soc": 50, "battery_watts": 2500}, 10)
        self.assertEqual(charging, {"state": "charging", "hours": 2.0})

    def test_days_json_and_csv(self):
        self.conn.execute(
            "INSERT INTO daily_summary(date,pv_energy_wh,load_energy_wh,inverter_1_pv_wh)"
            " VALUES(?,12000,8000,7000)",
            ((self.today - timedelta(days=1)).isoformat(),),
        )
        self.conn.commit()
        rows = self.client.get("/api/days?days=7").get_json()
        self.assertEqual(rows[0]["inverter_1_pv_wh"], 7000)
        self.assertIsNone(rows[0]["inverter_2_pv_wh"])
        csv_resp = self.client.get("/api/days.csv?days=7")
        self.assertEqual(csv_resp.mimetype, "text/csv")
        lines = csv_resp.get_data(as_text=True).strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("date,pv_energy_wh"))
        self.assertEqual(self.client.get("/days").status_code, 200)
