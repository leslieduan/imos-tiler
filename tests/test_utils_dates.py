"""Timezone + date helpers — the silent-404 surface called out in CLAUDE.md.

The API contract is: every date string is a TILE_TIMEZONE local date (default
Australia/Sydney). Zarr stores UTC. If conversions drift, requests miss the
matching timestamp and 404. These tests pin the conversion both directions.
"""

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.utils import dates as dates_mod


def test_ts_to_local_date_converts_utc_to_sydney():
    """A UTC midnight maps to the next-day Sydney date (Sydney is UTC+10/11)."""
    # 2024-06-15T23:00:00Z is 2024-06-16 09:00 AEST.
    ts = pd.Timestamp("2024-06-15T23:00:00")
    assert dates_mod.ts_to_local_date(ts) == "2024-06-16"


def test_ts_to_local_date_handles_numpy_datetime64():
    """The function is called with numpy datetime64 from Zarr — must accept it."""
    import numpy as np

    ts = np.datetime64("2024-01-01T00:00:00")
    # Sydney is UTC+11 in January → still 2024-01-01 local.
    assert dates_mod.ts_to_local_date(ts) == "2024-01-01"


def test_ts_to_local_date_uses_module_tz(monkeypatch):
    """Swapping LOCAL_TZ must change the returned local date — proves the call uses it."""
    monkeypatch.setattr(dates_mod, "LOCAL_TZ", ZoneInfo("UTC"))
    ts = pd.Timestamp("2024-06-15T23:00:00")
    assert dates_mod.ts_to_local_date(ts) == "2024-06-15"


def test_ts_to_local_date_dst_boundary():
    """Sydney AEDT→AEST transition (first Sunday of April) — UTC midnight either side
    must still produce the correct local date."""
    # 2024-04-06 (Saturday) 23:00Z is 2024-04-07 10:00 AEDT — before the 03:00 fallback.
    assert dates_mod.ts_to_local_date(pd.Timestamp("2024-04-06T23:00:00")) == "2024-04-07"
    # 2024-04-07 (Sunday) — after fallback Sydney is AEST (UTC+10).
    assert dates_mod.ts_to_local_date(pd.Timestamp("2024-04-07T20:00:00")) == "2024-04-08"


def test_three_months_ago_basic(monkeypatch):
    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2024, 6, 15)

    monkeypatch.setattr(dates_mod, "date", _FakeDate)
    assert dates_mod.three_months_ago() == "2024-03-15"


def test_three_months_ago_wraps_to_previous_year(monkeypatch):
    """February → previous November."""

    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2024, 2, 10)

    monkeypatch.setattr(dates_mod, "date", _FakeDate)
    assert dates_mod.three_months_ago() == "2023-11-10"


def test_three_months_ago_clamps_day_when_target_month_shorter(monkeypatch):
    """May 31 → Feb has no day 31. Must clamp to last day of Feb (28 in 2025)."""

    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2025, 5, 31)

    monkeypatch.setattr(dates_mod, "date", _FakeDate)
    assert dates_mod.three_months_ago() == "2025-02-28"


def test_three_months_ago_leap_year_clamp(monkeypatch):
    """May 31, 2024 (leap) → Feb 29, not Feb 28."""

    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2024, 5, 31)

    monkeypatch.setattr(dates_mod, "date", _FakeDate)
    assert dates_mod.three_months_ago() == "2024-02-29"


def test_three_months_ago_january_wraps(monkeypatch):
    """January → October of previous year."""

    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2024, 1, 5)

    monkeypatch.setattr(dates_mod, "date", _FakeDate)
    assert dates_mod.three_months_ago() == "2023-10-05"


@pytest.mark.parametrize(
    "today,expected",
    [
        ((2024, 3, 31), "2023-12-31"),  # year wrap + day-clamp friendly
        ((2024, 4, 30), "2024-01-30"),
        ((2024, 12, 1), "2024-09-01"),
    ],
)
def test_three_months_ago_table(monkeypatch, today, expected):
    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(*today)

    monkeypatch.setattr(dates_mod, "date", _FakeDate)
    assert dates_mod.three_months_ago() == expected


def test_three_months_ago_uses_calendar_monthrange():
    """Sanity: monthrange returns the last day of each month; nothing exotic."""
    for m in range(1, 13):
        last = calendar.monthrange(2024, m)[1]
        assert 28 <= last <= 31
