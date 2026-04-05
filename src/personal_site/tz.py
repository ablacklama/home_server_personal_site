from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def now_pacific() -> dt.datetime:
    """Current datetime in US/Pacific."""
    return dt.datetime.now(PACIFIC)


def today_pacific() -> dt.date:
    """Current date in US/Pacific."""
    return now_pacific().date()
