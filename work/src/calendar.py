"""Weekly calendar: Friday week keys, source date mappings, period boundaries, run windows
and completed-week filtering (NORM-007/008/009/013/019, REB-006, RUN-001/002)."""
from __future__ import annotations

import datetime as dt
import zoneinfo
from typing import Iterable, Optional

WEEK = dt.timedelta(days=7)
TZ = zoneinfo.ZoneInfo("Europe/Warsaw")


def friday_key(day: dt.date) -> dt.date:
    """Friday of the Monday-Sunday calendar week containing ``day`` (NORM-007).

    Also the Fama-French mapping of NORM-013: ``d - weekday(d) + 4`` (Saturday -> previous
    Friday, holiday Thursday -> following Friday of the same week)."""
    return day - dt.timedelta(days=day.weekday()) + dt.timedelta(days=4)


ff_week_key = friday_key


def week_start_to_key(week_start: dt.date) -> dt.date:
    """NORM-008: week_end = week_start + 4 calendar days; week_start must be a Monday."""
    if week_start.weekday() != 0:
        raise ValueError(f"week_start {week_start} is not a Monday")
    return week_start + dt.timedelta(days=4)


def week_monday(key: dt.date) -> dt.date:
    return key - dt.timedelta(days=4)


def week_sunday(key: dt.date) -> dt.date:
    """End of the calendar week identified by ``key`` (BTC close_date for that week)."""
    return key + dt.timedelta(days=2)


def is_friday(day: dt.date) -> bool:
    return day.weekday() == 4


def first_key_on_or_after(day: dt.date) -> dt.date:
    k = friday_key(day)
    return k if k >= day else k + WEEK


def last_key_on_or_before(day: dt.date) -> dt.date:
    k = friday_key(day)
    return k if k <= day else k - WEEK


def weekly_grid(first: dt.date, last: dt.date) -> list:
    out, k = [], first
    while k <= last:
        out.append(k)
        k += WEEK
    return out


def missing_keys(keys: Iterable[dt.date]) -> list:
    """Friday keys absent between the first and last key of an ascending key list (NORM-003)."""
    keys = list(keys)
    out = []
    for a, b in zip(keys, keys[1:]):
        k = a + WEEK
        while k < b:
            out.append(k)
            k += WEEK
    return out


def consecutive(prev_key: Optional[dt.date], key: dt.date) -> bool:
    return prev_key is not None and key - prev_key == WEEK


def period_id(key: dt.date, period: str) -> tuple:
    if period == "weekly":
        return (key,)
    if period == "monthly":
        return (key.year, key.month)
    if period == "quarterly":
        return (key.year, (key.month - 1) // 3)
    if period == "annually":
        return (key.year,)
    raise ValueError(period)


def is_first_week_of_period(prev_key: Optional[dt.date], key: dt.date, period: str) -> bool:
    """REB-006: first record whose Friday week_key falls in a new month/quarter/year."""
    if prev_key is None:
        return False
    return period_id(prev_key, period) != period_id(key, period)


def aggregate_daily_last(observations):
    """NORM-009: keep the last available observation of each Monday-Sunday week.
    ``observations`` is an ascending iterable of (date, payload); returns [(key, date, payload)]."""
    out = {}
    for day, payload in observations:
        k = friday_key(day)
        if k not in out or day >= out[k][0]:
            out[k] = (day, payload)
    return [(k, d, p) for k, (d, p) in sorted(out.items())]


def default_as_of() -> dt.date:
    """NORM-019 default: local execution date in Europe/Warsaw."""
    return dt.datetime.now(TZ).date()


def is_completed(week_key: dt.date, available_at: dt.date, as_of: dt.date,
                 end: Optional[dt.date] = None) -> bool:
    """NORM-019 with the Q-007 refinement: the week must not be after min(end, as_of) and
    its information must already be available at as_of (BTC: Sunday close_date)."""
    limit = as_of if end is None else min(end, as_of)
    return week_key <= limit and available_at <= as_of


def first_return_week(start: Optional[dt.date], earliest: dt.date) -> dt.date:
    """Q-014: first return week = first Friday key >= start (or the earliest common week)."""
    if start is None:
        return earliest
    return max(first_key_on_or_after(start), earliest)


def inception_date(first_return_week: dt.date) -> dt.date:
    """Q-014 (RESOLVED): allocation happens one week before the first return week."""
    return first_return_week - WEEK


def elapsed_days(first_return_week: dt.date, last_return_week: dt.date) -> int:
    """Q-014: elapsed_days = last_return_week - inception_date = 7 * number of return weeks."""
    return (last_return_week - inception_date(first_return_week)).days


def last_return_week(end: Optional[dt.date], latest: dt.date) -> dt.date:
    if end is None:
        return latest
    return min(last_key_on_or_before(end), latest)


def common_range(ranges: dict):
    """NORM-011: effective_start = max(source starts), effective_end = min(source ends).
    ``ranges`` maps role -> (first_key, last_key). Returns (start, end, truncations) where
    truncations lists (role, side, own_bound, effective_bound) for explicit reporting."""
    if not ranges:
        raise ValueError("no sources")
    start = max(a for a, _ in ranges.values())
    end = min(b for _, b in ranges.values())
    truncations = []
    for role in sorted(ranges):
        a, b = ranges[role]
        if a < start:
            truncations.append((role, "start", a, start))
        if b > end:
            truncations.append((role, "end", b, end))
    return start, end, truncations
