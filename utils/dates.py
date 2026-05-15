"""Pure date utility functions shared across routers and services."""

import calendar
import os
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd

# All date strings exposed by the API are local dates in this timezone.
# Requests must send them back unchanged.
LOCAL_TZ = ZoneInfo(os.environ.get("TILE_TIMEZONE", "Australia/Sydney"))


def ts_to_local_date(ts) -> str:
    """Convert a UTC numpy datetime64 or Timestamp to a local date string (YYYY-MM-DD)."""
    return str(pd.Timestamp(ts).tz_localize("UTC").tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"))


def three_months_ago() -> str:
    """Return the date three months before today as an ISO string."""
    today = date.today()
    month = today.month - 3
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()
