import unittest
from datetime import timedelta

import aggregator
import settings
from tests.helpers import cleanup_db, fresh_db
from timeutil import local_day_bounds, now_utc, to_iso, today_local


def insert_system_rows(conn, start, end, step=timedelta(minutes=5), pv=1000.0):
    rows = []
    t = start
    while t < end:
        rows.append((to_iso(t), pv, 500.0, 52.0, 80.0, 100.0, None, 2, "test"))
        t += step
    conn.executemany(
        "INSERT INTO system_samples(timestamp,total_pv_watts,total_load_watts,battery_voltage,"
        "battery_soc,battery_watts,grid_watts,inverter_count,source) VALUES(?,?,?,?,?,?,?,?,?)",
        rows,
    )
    inv = []
    for r in rows:
        for inverter_id in (1, 2):
            inv.append((r[0], inverter_id, pv / 2, 250.0, "B", "00"))
    conn.executemany(
        "INSERT INTO inverter_samples(timestamp,inverter_id,pv_watts,output_watts,mode,fault_code)"
        " VALUES(?,?,?,?,?,?)",
        inv,
    )
    conn.commit()
    return len(rows)


class IntegrateEnergyTests(unittest.TestCase):
    def rows(self, *pairs):
        return [{"timestamp": t, "w": w} for t, w in pairs]

    def test_constant_power(self):
        rows = self.rows(("2026-01-01T00:00:00+00:00", 600), ("2026-01-01T00:00:30+00:00", 600))
        self.assertAlmostEqual(aggregator.integrate_energy(rows, "w"), 5.0)

    def test_gap_is_skipped(self):
        rows = self.rows(("2026-01-01T00:00:00+00:00", 600), ("2026-01-01T00:10:00+00:00", 600))
        self.assertEqual(aggregator.integrate_energy(rows, "w"), 0.0)

    def test_transform_clamps(self):
        rows = self.rows(("2026-01-01T00:00:00+00:00", -600), ("2026-01-01T00:00:30+00:00", -600))
        self.assertEqual(aggregator.integrate_energy(rows, "w", aggregator.charge_only), 0.0)
        self.assertAlmostEqual(
            aggregator.integrate_energy(rows, "w", aggregator.discharge_only), 5.0
        )

    def test_fewer_than_two_samples(self):
        self.assertEqual(
            aggregator.integrate_energy(self.rows(("2026-01-01T00:00:00+00:00", 1)), "w"), 0.0
        )


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        aggregator.settings = settings
        self.conn.row_factory = aggregator.connect_db().row_factory

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def test_prune_runs_after_daily_summaries_exist(self):
        now = now_utc()
        insert_system_rows(self.conn, now - timedelta(days=10), now)
        conn = aggregator.connect_db()
        try:
            aggregator.aggregate_daily(conn)
            aggregator.aggregate_daily_inverters(conn)
            # This exact setup used to raise "missing system daily summaries".
            deleted = aggregator.prune_old_data(conn, raw_days=7, minute_days=365)
            self.assertGreater(deleted["system_samples"], 0)
            self.assertGreater(deleted["inverter_samples"], 0)

            cutoff_utc, _ = local_day_bounds(today_local() - timedelta(days=7))
            oldest = conn.execute("SELECT MIN(timestamp) FROM system_samples").fetchone()[0]
            self.assertGreaterEqual(oldest, to_iso(cutoff_utc))
        finally:
            conn.close()

    def test_prune_refuses_without_summaries(self):
        now = now_utc()
        insert_system_rows(self.conn, now - timedelta(days=10), now)
        conn = aggregator.connect_db()
        try:
            with self.assertRaises(RuntimeError):
                aggregator.prune_old_data(conn, raw_days=7, minute_days=365)
            count = conn.execute("SELECT COUNT(*) FROM system_samples").fetchone()[0]
            self.assertGreater(count, 0, "nothing may be deleted when the guard fires")
        finally:
            conn.close()

    def test_full_run_is_idempotent(self):
        now = now_utc()
        insert_system_rows(self.conn, now - timedelta(days=3), now)
        conn = aggregator.connect_db()
        try:
            aggregator.run(conn)
            first = conn.execute("SELECT * FROM daily_summary ORDER BY date").fetchall()
            aggregator.run(conn)
            second = conn.execute("SELECT * FROM daily_summary ORDER BY date").fetchall()
            self.assertEqual([tuple(r) for r in first], [tuple(r) for r in second])
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM minute_summary").fetchone()[0], 0)
            self.assertGreater(
                conn.execute("SELECT COUNT(*) FROM quarter_hour_summary").fetchone()[0], 0
            )
            self.assertGreater(
                conn.execute("SELECT COUNT(*) FROM monthly_summary").fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_daily_energy_matches_constant_power(self):
        # 1000 W constant for a whole completed day == 24 kWh.
        day = today_local() - timedelta(days=1)
        start, end = local_day_bounds(day)
        insert_system_rows(self.conn, start, end, step=timedelta(seconds=30))
        conn = aggregator.connect_db()
        try:
            aggregator.aggregate_daily(conn)
            row = conn.execute(
                "SELECT pv_energy_wh FROM daily_summary WHERE date=?", (day.isoformat(),)
            ).fetchone()
            hours = (end - start).total_seconds() / 3600
            self.assertAlmostEqual(row[0] / 1000.0, 1000 * hours / 1000.0, delta=0.05)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
