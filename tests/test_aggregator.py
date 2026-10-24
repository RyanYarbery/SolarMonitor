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

    def test_per_inverter_energy_lands_in_daily_summary(self):
        day = today_local() - timedelta(days=1)
        start, end = local_day_bounds(day)
        insert_system_rows(self.conn, start, end, step=timedelta(seconds=30), pv=1000.0)
        conn = aggregator.connect_db()
        try:
            aggregator.aggregate_daily(conn)
            aggregator.aggregate_daily_inverters(conn)
            row = conn.execute(
                "SELECT pv_energy_wh, inverter_1_pv_wh, inverter_2_pv_wh FROM daily_summary WHERE date=?",
                (day.isoformat(),),
            ).fetchone()
            self.assertAlmostEqual(row[1] + row[2], row[0], delta=1.0)
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


class PeriodAndWeatherTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def test_incremental_monthly_only_touches_recent_periods(self):
        self.conn.executemany(
            "INSERT INTO daily_summary(date, pv_energy_wh) VALUES(?, ?)",
            [("2025-01-05", 1000), ("2025-01-06", 1000), ("2026-10-20", 5000)],
        )
        self.conn.commit()
        conn = aggregator.connect_db()
        try:
            self.assertEqual(aggregator.aggregate_monthly(conn), 2)  # empty table -> full build
            # Tamper with January so we can tell whether it was recomputed.
            conn.execute("UPDATE monthly_summary SET pv_energy_wh = 1 WHERE month='2025-01'")
            conn.commit()
            n = aggregator.aggregate_monthly(conn, since=aggregator.date(2026, 10, 19))
            self.assertEqual(n, 1)
            jan = conn.execute(
                "SELECT pv_energy_wh FROM monthly_summary WHERE month='2025-01'"
            ).fetchone()[0]
            self.assertEqual(jan, 1)  # untouched
            oct_ = conn.execute(
                "SELECT pv_energy_wh FROM monthly_summary WHERE month='2026-10'"
            ).fetchone()[0]
            self.assertEqual(oct_, 5000)
        finally:
            conn.close()

    def test_performance_ratio_from_insolation(self):
        day = today_local() - timedelta(days=1)
        start, end = local_day_bounds(day)
        insert_system_rows(self.conn, start, end, step=timedelta(seconds=30), pv=2000.0)
        self.conn.execute("UPDATE system_config SET value='10' WHERE key='installed_pv_kw'")
        # 24 hourly rows of 200 W/m² -> 4800 Wh/m²
        hours = int((end - start).total_seconds() // 3600)
        self.conn.executemany(
            "INSERT INTO weather_samples(timestamp, ghi_wm2, source) VALUES(?, 200, 't')",
            [(to_iso(start + timedelta(hours=i + 1)),) for i in range(hours)],
        )
        self.conn.commit()
        conn = aggregator.connect_db()
        try:
            aggregator.aggregate_daily(conn)
            row = conn.execute(
                "SELECT insolation_wh_m2, performance_ratio FROM daily_summary WHERE date=?",
                (day.isoformat(),),
            ).fetchone()
            self.assertAlmostEqual(row[0], 200 * hours)
            # PR = 2000 W * hours / (10 kW * 200 * hours) = 1.0
            self.assertAlmostEqual(row[1], 1.0, delta=0.01)
        finally:
            conn.close()

    def test_partial_weather_coverage_gives_no_ratio(self):
        day = today_local() - timedelta(days=1)
        start, end = local_day_bounds(day)
        insert_system_rows(self.conn, start, end)
        self.conn.execute("UPDATE system_config SET value='10' WHERE key='installed_pv_kw'")
        self.conn.execute(
            "INSERT INTO weather_samples(timestamp, ghi_wm2) VALUES(?, 500)",
            (to_iso(start + timedelta(hours=12)),),
        )
        self.conn.commit()
        conn = aggregator.connect_db()
        try:
            aggregator.aggregate_daily(conn)
            row = conn.execute(
                "SELECT insolation_wh_m2, performance_ratio FROM daily_summary"
            ).fetchone()
            self.assertIsNone(row[0])
            self.assertIsNone(row[1])
        finally:
            conn.close()

    def test_stale_check(self):
        from unittest import mock

        import alerts

        now = now_utc()
        insert_system_rows(self.conn, now - timedelta(hours=2), now - timedelta(hours=1))
        conn = aggregator.connect_db()
        try:
            with mock.patch.object(alerts, "send", return_value=True) as send:
                age = aggregator.check_telemetry_age(conn, stale_after=600)
                self.assertGreater(age, 3000)
                send.assert_called_once()
                self.assertIn("stale", send.call_args[0][0].lower())
                # Within cooldown: no second notification.
                aggregator.check_telemetry_age(conn, stale_after=600)
                send.assert_called_once()
                # Fresh data clears it.
                insert_system_rows(self.conn, now - timedelta(seconds=20), now)
                aggregator.check_telemetry_age(conn, stale_after=600)
                self.assertEqual(send.call_count, 2)
                self.assertIn("resumed", send.call_args[0][0].lower())
        finally:
            conn.close()


class RebuildDaysTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        cleanup_db(self.path)

    def test_rebuild_replaces_stale_buckets_and_daily(self):
        day = today_local() - timedelta(days=2)
        start, end = local_day_bounds(day)
        insert_system_rows(self.conn, start, end, step=timedelta(seconds=30), pv=1000.0)
        self.conn.execute(
            "INSERT INTO minute_summary(timestamp,pv_watts_avg,sample_count) VALUES(?,999,1)",
            (to_iso(start + timedelta(hours=1)),),
        )
        self.conn.execute(
            "INSERT INTO daily_summary(date,pv_energy_wh) VALUES(?,1)", (day.isoformat(),)
        )
        self.conn.commit()
        conn = aggregator.connect_db()
        try:
            self.assertEqual(aggregator.rebuild_days(conn, [day]), 1)
            minute = conn.execute(
                "SELECT pv_watts_avg FROM minute_summary WHERE timestamp=?",
                (to_iso(start + timedelta(hours=1)),),
            ).fetchone()[0]
            self.assertEqual(minute, 1000.0)
            daily = conn.execute(
                "SELECT pv_energy_wh FROM daily_summary WHERE date=?", (day.isoformat(),)
            ).fetchone()[0]
            hours = (end - start).total_seconds() / 3600
            self.assertAlmostEqual(daily, 1000 * hours, delta=50)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM monthly_summary").fetchone()[0], 1)
        finally:
            conn.close()

    def test_cli_rebuild(self):
        day = today_local() - timedelta(days=2)
        start, end = local_day_bounds(day)
        insert_system_rows(self.conn, start, end)
        self.conn.commit()
        aggregator.main(["--rebuild", day.isoformat()])
        conn = aggregator.connect_db()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0], 1)
        finally:
            conn.close()
