"""Information availability (META-003, NORM-016/017, SEM-009).

Time model per weekly key K (Friday of a Monday-Sunday week):
  * decision time of week K     = end of week K-1 (Sunday K-5): start-of-week trades may use
                                  only information with available_at <= that Sunday;
  * evaluation time of week K   = end of week K (Sunday K+2): end-of-week signal evaluation
                                  may use information with available_at <= that Sunday;
  * execution week              = confirm week + 7*delay, delay >= 1 (DELAY-002).
Stocks/FF/gold observations are available at their Friday close (available_at = K);
BTC observations only at close_date (Sunday, K+2).
"""
from __future__ import annotations

import bisect
import datetime as dt

from .calendar import WEEK, week_sunday
from .errors import ConfigError, LookAheadError


def evaluation_time(week_key: dt.date) -> dt.date:
    return week_sunday(week_key)


def decision_time(week_key: dt.date) -> dt.date:
    return week_sunday(week_key - WEEK)


def execution_week(confirm_week: dt.date, delay: int) -> dt.date:
    if not isinstance(delay, int) or delay < 1:
        raise ConfigError(f"delay must be a positive integer (DELAY-002), got {delay!r}")
    return confirm_week + delay * WEEK


def assert_available(point, at: dt.date, context: str = "") -> None:
    if point.available_at > at:
        raise LookAheadError(
            f"{context}observation for week {point.week_key} is available at "
            f"{point.available_at}, requested at {at}")


class DataView:
    """Read-only view of a point series that refuses access to not-yet-available data."""

    def __init__(self, points):
        self._points = tuple(points)
        self._avail = [p.available_at for p in self._points]
        if self._avail != sorted(self._avail):
            raise ValueError("points must be ordered by availability")
        self._by_key = {p.week_key: p for p in self._points}

    def visible(self, at: dt.date) -> tuple:
        """All points with available_at <= at."""
        return self._points[:bisect.bisect_right(self._avail, at)]

    def get(self, week_key: dt.date, at: dt.date):
        p = self._by_key.get(week_key)
        if p is None:
            return None
        assert_available(p, at)
        return p
