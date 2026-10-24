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
