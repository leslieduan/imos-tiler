"""Timezone + date helpers — the silent-404 surface called out in CLAUDE.md.

The API contract is: every date string is a TILE_TIMEZONE local date (default
Australia/Sydney). Zarr stores UTC. If conversions drift, requests miss the
matching timestamp and 404. These tests pin the conversion both directions.
"""

from zoneinfo import ZoneInfo

import pandas as pd

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
