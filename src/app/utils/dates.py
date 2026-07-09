"""Pure date utility functions shared across routers and services."""

from zoneinfo import ZoneInfo

import pandas as pd

from app.config.settings import TILE_TIMEZONE

# All date strings exposed by the API are local dates in this timezone.
# Requests must send them back unchanged.
LOCAL_TZ = ZoneInfo(TILE_TIMEZONE)


def ts_to_local_date(ts) -> str:
    """Convert a UTC numpy datetime64 or Timestamp to a local date string (YYYY-MM-DD)."""
    return str(pd.Timestamp(ts).tz_localize("UTC").tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"))
