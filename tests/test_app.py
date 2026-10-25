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


class FallbackAndBucketTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        settings.LIVE_PATH = Path(tempfile.mktemp(suffix=".json"))
        self.client = webapp.app.test_client()
        self.now = now_utc()

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def _sample(self, ts, pv):
        self.conn.execute(
            "INSERT INTO system_samples(timestamp,total_pv_watts,total_load_watts,battery_soc,"
            "battery_watts,inverter_count,source) VALUES(?,?,400,70,50,2,'test')",
            (ts, pv),
        )

    def test_latest_without_live_file_derives_grid_and_fault(self):
        ts = to_iso(self.now)
        self._sample(ts, 800)
        self.conn.executemany(
            "INSERT INTO inverter_samples(timestamp,inverter_id,grid_voltage,mode,fault_code)"
            " VALUES(?,?,?,'L',?)",
            [(ts, 1, 121.0, "00"), (ts, 2, 0.0, "09")],
        )
        self.conn.commit()
        d = self.client.get("/api/latest").get_json()
        self.assertTrue(d["grid_present"])
        self.assertEqual(d["grid_voltage"], 121.0)
        self.assertTrue(d["any_fault"])

    def test_latest_without_inverters_leaves_grid_unknown(self):
        self._sample(to_iso(self.now), 800)
        self.conn.commit()
        d = self.client.get("/api/latest").get_json()
        self.assertIsNone(d["grid_present"])
        self.assertFalse(d["any_fault"])

    def test_summary_reports_todays_pv_peak(self):
        from timeutil import local_day_bounds, today_local

        start, _ = local_day_bounds(today_local())
        if self.now - start < timedelta(minutes=5):
            self.skipTest("too close to local midnight for a same-day peak")
        peak_ts = to_iso(self.now - timedelta(minutes=2))
        self._sample(to_iso(self.now - timedelta(minutes=3)), 500)
        self._sample(peak_ts, 2500)
        self._sample(to_iso(self.now - timedelta(minutes=1)), 2500)
        self.conn.commit()
        s = self.client.get("/api/summary").get_json()
        self.assertEqual(s["today"]["pv_peak_watts"], 2500)
        self.assertEqual(s["today"]["pv_peak_time"], peak_ts)
        self.assertFalse(s["has_coordinates"])

    def _days(self, n):
        from timeutil import today_local

        for i in range(1, n + 1):
            day = today_local() - timedelta(days=i)
            self.conn.execute(
                "INSERT INTO daily_summary(date,pv_energy_wh,load_energy_wh,battery_soc_min,"
                "battery_soc_max) VALUES(?,?,8000,?,95)",
                (day.isoformat(), 10000 + i, 20 + i % 10),
            )
        self.conn.commit()

    def test_history_daily_buckets_by_day_week_and_month(self):
        self._days(400)
        day = self.client.get("/api/history?hours=" + str(24 * 90)).get_json()
        self.assertEqual(day["bucket"], "day")
        self.assertEqual(len(day["points"]), 90)
        self.assertEqual(day["points"][0]["battery_soc_min"], day["points"][0]["battery_soc_min"])
        week = self.client.get("/api/history?hours=" + str(24 * 365)).get_json()
        self.assertEqual(week["bucket"], "week")
        self.assertLessEqual(len(week["points"]), 54)
        # Every full week starts on a Monday and holds seven days.
        from datetime import date

        full = [p for p in week["points"] if p["days"] == 7]
        self.assertGreater(len(full), 48)
        self.assertTrue(all(date.fromisoformat(p["timestamp"][:10]).weekday() == 0 for p in full))
        self.assertLessEqual(min(p["battery_soc_min"] for p in full), 21)
        month = self.client.get("/api/history?hours=" + str(24 * 365 * 3)).get_json()
        self.assertEqual(month["bucket"], "month")
        self.assertLessEqual(len(month["points"]), 15)
        self.assertTrue(all(p["timestamp"][8:10] == "01" for p in month["points"][1:]))
