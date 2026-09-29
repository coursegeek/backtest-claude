import datetime as dt

import pytest

from src.calendar import (aggregate_daily_last, first_return_week, friday_key, is_completed,
                          is_first_week_of_period, last_return_week, missing_keys,
                          week_start_to_key, weekly_grid)

D = dt.date.fromisoformat


def test_friday_key_same_calendar_week():
    """NORM-007: every day of a Monday-Sunday week maps to its Friday."""
    for day in range(7):
        assert friday_key(D("2024-01-01") + dt.timedelta(days=day)) == D("2024-01-05")


def test_week_start_plus_4():
    """NORM-008."""
    assert week_start_to_key(D("2026-09-21")) == D("2026-09-25")
    with pytest.raises(ValueError):
        week_start_to_key(D("2026-09-22"))


def test_daily_to_weekly_last():
    """NORM-009: last available observation of the calendar week (holiday Friday -> Thursday)."""
    obs = [(D("2024-03-25"), 1), (D("2024-03-28"), 2), (D("2024-04-01"), 3), (D("2024-04-05"), 4)]
    assert aggregate_daily_last(obs) == [(D("2024-03-29"), D("2024-03-28"), 2),
                                         (D("2024-04-05"), D("2024-04-05"), 4)]


def test_period_boundaries_by_friday_key():
    """REB-006 building block: Monday 2020-12-28 / Friday 2021-01-01 starts 2021."""
    prev, k = D("2020-12-25"), D("2021-01-01")
    assert is_first_week_of_period(prev, k, "annually")
    assert is_first_week_of_period(prev, k, "quarterly") and is_first_week_of_period(prev, k, "monthly")
    assert not is_first_week_of_period(D("2021-01-08"), D("2021-01-15"), "monthly")
    assert not is_first_week_of_period(None, k, "annually")


def test_start_maps_to_first_week_key():
    """RUN-001 / Q-014: first return week = first Friday key >= start."""
    assert first_return_week(D("1971-01-01"), D("1926-07-02")) == D("1971-01-01")
    assert first_return_week(D("2018-01-01"), D("1926-07-02")) == D("2018-01-05")
    assert first_return_week(D("2018-01-06"), D("1926-07-02")) == D("2018-01-12")
    assert first_return_week(None, D("1926-07-02")) == D("1926-07-02")


def test_end_defaults_to_last_common_week():
    """RUN-002."""
    assert last_return_week(None, D("2026-08-28")) == D("2026-08-28")
    assert last_return_week(D("2026-07-31"), D("2026-08-28")) == D("2026-07-31")
    assert last_return_week(D("2026-08-02"), D("2026-08-28")) == D("2026-07-31")


def test_completion_rule_with_availability():
    """NORM-019 + Q-007: Friday key <= min(end, as_of) and information available at as_of."""
    k = D("2026-09-25")
    assert not is_completed(k, k, D("2026-09-22"))
    assert is_completed(k, k, D("2026-09-25"))
    assert not is_completed(k, k + dt.timedelta(days=2), D("2026-09-26"))   # BTC Sunday close
    assert is_completed(k, k + dt.timedelta(days=2), D("2026-09-27"))
    assert not is_completed(k, k, D("2026-09-29"), end=D("2026-09-20"))


def test_missing_keys_and_grid():
    keys = [D("1933-02-24"), D("1933-03-03"), D("1933-03-17")]
    assert missing_keys(keys) == [D("1933-03-10")]
    assert weekly_grid(D("1933-03-03"), D("1933-03-17")) == [D("1933-03-03"), D("1933-03-10"), D("1933-03-17")]


def test_inception_and_elapsed_days():
    """Q-014 (RESOLVED): first return week = first Friday key >= start (a Friday start is its
    own first return week), last = last Friday key <= end, inception = first - 7 days,
    elapsed_days = last - inception = 7 * number of return weeks."""
    from src.calendar import elapsed_days, first_return_week, inception_date, last_return_week
    early = dt.date(1900, 1, 5)
    assert first_return_week(dt.date(2018, 1, 5), early) == dt.date(2018, 1, 5)     # Friday
    assert first_return_week(dt.date(2018, 1, 6), early) == dt.date(2018, 1, 12)
    first = first_return_week(dt.date(2018, 1, 1), early)
    last = last_return_week(dt.date(2026, 7, 31), dt.date(2030, 1, 4))
    assert (first, last, inception_date(first)) == (dt.date(2018, 1, 5), dt.date(2026, 7, 31),
                                                   dt.date(2017, 12, 29))
    n = (last - first).days // 7 + 1
    assert elapsed_days(first, last) == 7 * n == 3136
