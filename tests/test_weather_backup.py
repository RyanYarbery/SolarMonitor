import gzip
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import backup
import weather
from tests.helpers import cleanup_db, fresh_db
from timeutil import UTC


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def payload(self, now):
        start = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
        times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(6)]
        return {
            "hourly": {
                "time": times,
                "shortwave_radiation": [100, 200, 300, None, 500, 600],
                "cloud_cover": [10, 20, 30, 40, 50, 60],
                "temperature_2m": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            }
        }

    def test_only_completed_hours_are_stored(self):
        now = datetime(2026, 10, 23, 18, 30, tzinfo=UTC)
        rows = weather.rows_from_payload(self.payload(now), now=now)
        # hours starting 15,16,17 are complete (end <= 18:30); 18 ends at 19:00 (future)
        self.assertEqual(
            [r[0] for r in rows],
            [
                "2026-10-23T16:00:00+00:00",
                "2026-10-23T17:00:00+00:00",
                "2026-10-23T18:00:00+00:00",
            ],
        )
        self.assertEqual(weather.store(self.conn, rows), 3)
        self.assertEqual(weather.store(self.conn, rows), 3)  # upsert, no duplicates
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM weather_samples").fetchone()[0], 3)

    def test_coordinates_unset(self):
        self.assertIsNone(weather.site_coordinates(self.conn))
        self.conn.execute("UPDATE system_config SET value='36.1' WHERE key='latitude'")
        self.conn.execute("UPDATE system_config SET value='-95.9' WHERE key='longitude'")
        self.assertEqual(weather.site_coordinates(self.conn), (36.1, -95.9))

    def test_url(self):
        url = weather.build_url(36.1, -95.9, 2)
        self.assertIn("latitude=36.1000", url)
        self.assertIn("past_days=2", url)
        self.assertIn("shortwave_radiation", url)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        self.conn.execute(
            "INSERT INTO system_samples(timestamp,total_pv_watts) VALUES('2026-01-01T00:00:00+00:00', 1)"
        )
        self.conn.commit()
        self.dest = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def test_backup_is_valid_and_rotates(self):
        made = []
        for _ in range(3):
            final, _removed = backup.make_backup(self.path, self.dest, keep=2)
            made.append(final)
            import time

            time.sleep(1.1)  # distinct timestamps in filenames
        remaining = sorted(self.dest.glob("*.db.gz"))
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(made[0], remaining)

        restored = self.dest / "restored.db"
        with gzip.open(remaining[-1], "rb") as f_in, restored.open("wb") as f_out:
            f_out.write(f_in.read())
        conn = sqlite3.connect(restored)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM system_samples").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_command_hook(self):
        marker = self.dest / "shipped.txt"
        final, _ = backup.make_backup(
            self.path, self.dest, keep=5, command=f"echo {{path}} > {marker}"
        )
        self.assertEqual(marker.read_text().strip(), str(final))


if __name__ == "__main__":
    unittest.main()
