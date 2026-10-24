"""Timestamp conventions.

All sample timestamps are stored as ISO-8601 UTC strings with an explicit
"+00:00" offset, e.g. "2026-10-23T18:30:00+00:00". They sort correctly as
text, parse with datetime.fromisoformat on Python 3.7+, and SQLite's date
functions understand them. Naive local time was used before schema v3 and
produced duplicate keys during the autumn DST change.

Daily, monthly and yearly summaries are keyed on *local* calendar dates,
because a solar day is a local concept.
"""

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt):
    """Serialise an aware datetime as UTC ISO text."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to to_iso")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value):
    """Parse stored text. Legacy naive values are interpreted as local time."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> local -> aware
    return dt.astimezone(UTC)


def local_date(dt):
    """Local calendar date of an aware datetime."""
    return dt.astimezone().date()


def local_day_bounds(day):
    """UTC start/end of a local calendar day. Handles 23h/25h DST days."""
    start = datetime.combine(day, time()).astimezone()
    end = datetime.combine(day + timedelta(days=1), time()).astimezone()
    return start.astimezone(UTC), end.astimezone(UTC)


def today_local():
    return datetime.now().date()


def floor_minute(dt):
    return dt.replace(second=0, microsecond=0)


def floor_quarter_hour(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def naive_local_to_utc(value, fold=0):
    """Convert a legacy naive local ISO string to UTC ISO text."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        return to_iso(dt)
    return to_iso(dt.replace(fold=fold).astimezone())


__all__ = [
    "UTC",
    "date",
    "now_utc",
    "to_iso",
    "parse_iso",
    "local_date",
    "local_day_bounds",
    "today_local",
    "floor_minute",
    "floor_quarter_hour",
    "naive_local_to_utc",
]
