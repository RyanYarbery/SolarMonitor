import os
import time
import unittest
from datetime import datetime, timezone

import timeutil
from tests.helpers import ROOT  # noqa: F401


class TimeUtilTests(unittest.TestCase):
    def test_to_iso_has_offset(self):
        dt = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(timeutil.to_iso(dt), "2026-10-23T18:00:00+00:00")

    def test_to_iso_rejects_naive(self):
        with self.assertRaises(ValueError):
            timeutil.to_iso(datetime(2026, 1, 1))

    def test_parse_round_trip(self):
        s = "2026-10-23T18:00:00+00:00"
        self.assertEqual(timeutil.to_iso(timeutil.parse_iso(s)), s)

    def test_iso_sorts_lexicographically(self):
        a = timeutil.to_iso(datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc))
        b = timeutil.to_iso(datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.assertLess(a, b)


class DstTests(unittest.TestCase):
    """Run with TZ=America/Chicago to exercise the autumn fall-back."""

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

    def test_fall_back_day_is_25_hours(self):
        start, end = timeutil.local_day_bounds(timeutil.date(2026, 11, 1))
        self.assertEqual((end - start).total_seconds(), 25 * 3600)

    def test_spring_forward_day_is_23_hours(self):
        start, end = timeutil.local_day_bounds(timeutil.date(2026, 3, 8))
        self.assertEqual((end - start).total_seconds(), 23 * 3600)

    def test_ambiguous_local_hour_maps_to_distinct_utc_with_fold(self):
        first = timeutil.naive_local_to_utc("2026-11-01T01:30:00", fold=0)
        second = timeutil.naive_local_to_utc("2026-11-01T01:30:00", fold=1)
        self.assertNotEqual(first, second)
        self.assertEqual(first, "2026-11-01T06:30:00+00:00")
        self.assertEqual(second, "2026-11-01T07:30:00+00:00")


if __name__ == "__main__":
    unittest.main()
