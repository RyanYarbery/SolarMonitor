import os
import sqlite3
import time
import unittest

import migrate_v3
import settings
from tests.helpers import cleanup_db, fresh_db


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()
        self.conn, self.path = fresh_db()
        # Make it look like a v2 database: drop the new columns, use naive local timestamps.
        self.conn.executescript(
            """
            ALTER TABLE inverter_samples DROP COLUMN mode;
            ALTER TABLE inverter_samples DROP COLUMN fault_code;
            DROP TABLE daily_inverter_summary;
            UPDATE system_config SET value='2' WHERE key='schema_version';
            INSERT INTO system_config(key,value) VALUES('raw_retention_days','7');
            """
        )
        rows = [
            ("2026-10-20T12:00:00", 100),
            ("2026-11-01T01:30:00", 200),  # ambiguous: occurs twice in local time
            ("2026-11-01T01:30:05", 300),
        ]
        self.conn.executemany(
            "INSERT INTO system_samples(timestamp,total_pv_watts) VALUES(?,?)", rows
        )
        self.conn.executemany(
            "INSERT INTO inverter_samples(timestamp,inverter_id,pv_watts) VALUES(?,1,?)", rows
        )
        self.conn.execute(
            "INSERT INTO daily_summary(date,pv_peak_time) VALUES('2026-10-20','2026-10-20T13:00:00')"
        )
        self.conn.commit()
        self.conn.close()

    def tearDown(self):
        cleanup_db(self.path)
        if self.old_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def test_migration(self):
        self.assertEqual(migrate_v3.main(), 0)
        self.assertTrue(self.path.with_name(self.path.name + ".before-v3").exists())

        conn = sqlite3.connect(settings.DB_PATH)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(inverter_samples)")}
        self.assertIn("mode", cols)
        self.assertIn("fault_code", cols)
        self.assertTrue(migrate_v3.table_exists(conn, "daily_inverter_summary"))

        ts = [r[0] for r in conn.execute("SELECT timestamp FROM system_samples ORDER BY timestamp")]
        self.assertEqual(ts[0], "2026-10-20T17:00:00+00:00")  # CDT is UTC-5
        self.assertTrue(all(t.endswith("+00:00") for t in ts))

        peak = conn.execute("SELECT pv_peak_time FROM daily_summary").fetchone()[0]
        self.assertEqual(peak, "2026-10-20T18:00:00+00:00")

        cfg = dict(conn.execute("SELECT key,value FROM system_config"))
        self.assertEqual(cfg["schema_version"], "3")
        self.assertNotIn("raw_retention_days", cfg)

        # Re-running is harmless.
        self.assertEqual(migrate_v3.main(), 0)
        again = [
            r[0] for r in conn.execute("SELECT timestamp FROM system_samples ORDER BY timestamp")
        ]
        self.assertEqual(ts, again)
        conn.close()


if __name__ == "__main__":
    unittest.main()


class DstFallBackRepairTests(unittest.TestCase):
    """Simulate the old collector running through 2026-11-01 in America/Chicago.

    Local 01:00-01:59 happens twice. The old code wrote naive timestamps, so
    the second pass partly collided (crashing the collector) and otherwise
    interleaved with the first. The migration must put the second pass an
    hour later in UTC and rebuild that day's roll-ups.
    """

    def setUp(self):
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()
        self.conn, self.path = fresh_db()
        self.conn.executescript(
            "ALTER TABLE inverter_samples DROP COLUMN mode;"
            "UPDATE system_config SET value='2' WHERE key='schema_version';"
        )
        from datetime import datetime, timedelta

        rows = []
        t = datetime(2026, 11, 1, 0, 0, 0)  # naive local
        # first pass: 00:00 -> 01:59:50 CDT (pv 100), ids ascending
        while t < datetime(2026, 11, 1, 2, 0, 0):
            rows.append((t.isoformat(), 100.0))
            t += timedelta(seconds=10)
        # clock falls back: second pass 01:00:05 -> 01:59:55 CST (pv 200), offset by 5 s
        t = datetime(2026, 11, 1, 1, 0, 5)
        while t < datetime(2026, 11, 1, 2, 0, 0):
            rows.append((t.isoformat(), 200.0))
            t += timedelta(seconds=10)
        # then onward in CST (pv 300)
        t = datetime(2026, 11, 1, 2, 0, 5)
        while t < datetime(2026, 11, 1, 3, 0, 0):
            rows.append((t.isoformat(), 300.0))
            t += timedelta(seconds=10)
        self.conn.executemany(
            "INSERT INTO system_samples(timestamp,total_pv_watts,total_load_watts,battery_soc,battery_watts)"
            " VALUES(?,?,500,50,0)",
            rows,
        )
        self.conn.executemany(
            "INSERT INTO inverter_samples(timestamp,inverter_id,pv_watts,output_watts) VALUES(?,1,?,250)",
            rows,
        )
        # Old aggregator merged both passes into one 01:xx bucket set; seed a wrong bucket.
        self.conn.execute(
            "INSERT INTO minute_summary(timestamp,pv_watts_avg,sample_count) VALUES('2026-11-01T01:30:00',150,12)"
        )
        self.conn.commit()
        self.conn.close()

    def tearDown(self):
        cleanup_db(self.path)
        if self.old_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def test_second_pass_lands_an_hour_later_and_rollups_rebuilt(self):
        self.assertEqual(migrate_v3.main(), 0)
        conn = sqlite3.connect(settings.DB_PATH)
        # First pass 01:30:00 CDT -> 06:30:00Z with pv 100; second pass 01:30:05 CST -> 07:30:05Z pv 200
        self.assertEqual(
            conn.execute(
                "SELECT total_pv_watts FROM system_samples WHERE timestamp='2026-11-01T06:30:00+00:00'"
            ).fetchone()[0],
            100.0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT total_pv_watts FROM system_samples WHERE timestamp='2026-11-01T07:30:05+00:00'"
            ).fetchone()[0],
            200.0,
        )
        # Nothing was dropped: every row got a unique UTC timestamp.
        total = conn.execute("SELECT COUNT(*) FROM system_samples").fetchone()[0]
        self.assertEqual(total, 720 + 360 + 360)
        # 02:00 CST onward -> 08:00Z
        self.assertEqual(
            conn.execute(
                "SELECT MIN(timestamp) FROM system_samples WHERE total_pv_watts=300"
            ).fetchone()[0],
            "2026-11-01T08:00:05+00:00",
        )
        # The merged minute bucket was replaced by correct per-hour buckets.
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM minute_summary WHERE timestamp='2026-11-01T01:30:00'"
            ).fetchone()
        )
        first = conn.execute(
            "SELECT pv_watts_avg, sample_count FROM minute_summary WHERE timestamp='2026-11-01T06:30:00+00:00'"
        ).fetchone()
        second = conn.execute(
            "SELECT pv_watts_avg, sample_count FROM minute_summary WHERE timestamp='2026-11-01T07:30:00+00:00'"
        ).fetchone()
        self.assertEqual(tuple(first), (100.0, 6))
        self.assertEqual(tuple(second), (200.0, 6))
        # Daily summary exists for the (25-hour) local day with sane energy:
        # 2 h @100 + 1 h @200 + 1 h @300 ~= 700 Wh (minus gap edges)
        pv_wh = conn.execute(
            "SELECT pv_energy_wh FROM daily_summary WHERE date='2026-11-01'"
        ).fetchone()[0]
        self.assertAlmostEqual(pv_wh, 700, delta=15)
        conn.close()


class AssignFoldsTests(unittest.TestCase):
    def setUp(self):
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()

    def tearDown(self):
        if self.old_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def test_ntp_step_outside_ambiguity_is_ignored(self):
        rows = [
            (1, "2026-11-01T00:10:00"),
            (2, "2026-11-01T00:09:00"),
            (3, "2026-11-01T01:10:00"),
            (4, "2026-11-01T01:05:00"),
            (5, "2026-11-01T02:00:00"),
        ]
        folds = migrate_v3.assign_folds(rows)
        self.assertEqual(folds, {1: 0, 2: 0, 3: 0, 4: 1, 5: 0})
