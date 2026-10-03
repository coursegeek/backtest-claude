"""Rolling windows (MET-022, MET-023) as pure functions on weekly paths: calendar-year targets
with 29 Feb -> 28 Feb, start = last path point on or before the target (no interpolation, no
fill, no partial window), total return, CAGR (MET-003), max drawdown (MET-009), worst window
and its tie-break, ordering, no look-ahead. Values are computed by hand in the comments."""
import datetime as dt
import random

import pytest

from src.calendar import add_years
from src.metrics import (PathSeries, RollingWindow, max_drawdown, path_points,
                         rolling_metrics, rolling_window_bounds, window_max_drawdown,
                         worst_rolling_return)

D = dt.date.fromisoformat
WEEK = dt.timedelta(days=7)


def fridays(first: str, n: int, skip=()) -> tuple:
    k0 = D(first)
    assert k0.weekday() == 4
    return tuple(k0 + i * WEEK for i in range(n) if k0 + i * WEEK not in {D(s) for s in skip})


def path(keys, navs, nav_start=100.0) -> PathSeries:
    return PathSeries(nav_start, tuple(keys), tuple(0.0 for _ in keys), tuple(navs))


def test_one_year_window_by_hand():
    """MET-022: first week 2019-01-04 -> inception 2018-12-28 (point 0, NAV_start 100).
    Window end 2019-12-27: target 2018-12-27 precedes every point -> no window. End
    2020-01-03: target 2019-01-03 -> start = inception (last point <= target), 371 days,
    53 weekly intervals; NAV 100 -> 130 => total return 0.3, CAGR 1.3**(365.2425/371) - 1."""
    keys = fridays("2019-01-04", 60)
    navs = [100.0 + i for i in range(1, 60)] + [130.0]     # week 53 (2020-01-03) set below
    navs[52] = 130.0
    r = rolling_metrics(path(keys, navs), path(keys, navs), horizons=(1,))
    first = r.windows[0]
    assert (first.window_start, first.window_end) == (D("2018-12-28"), D("2020-01-03"))
    assert (first.elapsed_days, first.weeks) == (371, 53)
    assert first.pre_tax_start_nav == 100.0 and first.pre_tax_end_nav == 130.0
    assert first.pre_tax_total_return == 130.0 / 100.0 - 1.0
    assert first.pre_tax_cagr == pytest.approx(1.3 ** (365.2425 / 371) - 1, rel=1e-15)
    assert D("2019-12-27") not in {w.window_end for w in r.windows}
    second = r.windows[1]                                   # end 2020-01-10 -> start 2019-01-04
    assert (second.window_start, second.window_end, second.elapsed_days) == (
        D("2019-01-04"), D("2020-01-10"), 371)
    assert second.pre_tax_start_nav == navs[0]
    assert len(r.windows) == 60 - 52                        # ends 2020-01-03 .. 2020-02-21


def test_calendar_year_shift_and_leap_day():
    """29 Feb -> 28 Feb; the actual start is the last Friday path point on or before the target.
    2008-02-29 is a Friday: end 2009-03-06 -> target 2008-03-06 -> start 2008-02-29;
    end 2009-02-27 -> target 2008-02-27 -> start 2008-02-22; end 2008-02-29 ->
    target 2007-02-28 (Wed) -> start 2007-02-23."""
    assert add_years(D("2008-02-29"), -1) == D("2007-02-28")
    assert add_years(D("2012-02-29"), -4) == D("2008-02-29")
    keys = fridays("2006-01-06", 220)
    dates, _ = path_points(path(keys, [100.0] * len(keys)))
    bounds = {dates[j]: dates[i] for i, j in rolling_window_bounds(dates, 1)}
    assert bounds[D("2009-03-06")] == D("2008-02-29")
    assert bounds[D("2009-02-27")] == D("2008-02-22")
    assert bounds[D("2008-02-29")] == D("2007-02-23")
    for end, start in bounds.items():                       # never shorter than the horizon
        assert start <= add_years(end, -1) < start + WEEK


def test_calendar_gap_is_not_filled():
    """A missing Friday at the target: the window starts at the last existing point before it
    (no forward fill); elapsed_days is the real span (378 days)."""
    keys = fridays("2010-01-01", 70, skip=("2010-03-05",))
    dates, _ = path_points(path(keys, [100.0] * len(keys)))
    bounds = {dates[j]: dates[i] for i, j in rolling_window_bounds(dates, 1)}
    assert bounds[D("2011-03-04")] == D("2010-02-26")      # target 2010-03-04, 03-05 missing
    r = rolling_metrics(path(keys, [100.0] * len(keys)), path(keys, [100.0] * len(keys)), (1,))
    w = next(x for x in r.windows if x.window_end == D("2011-03-04"))
    assert w.elapsed_days == 371 and w.weeks == 52          # 53 calendar weeks, 52 retained
    w = next(x for x in r.windows if x.window_end == D("2011-03-11"))
    assert (w.window_start, w.elapsed_days, w.weeks) == (D("2010-02-26"), 378, 53)


def test_rolling_max_drawdown_includes_start():
    """MET-009 on the window path: start 100, peak 120, trough 90, end 110 -> 1 - 90/120 = 25%;
    a window starting at the peak keeps it in the running maximum."""
    navs = [100.0, 120.0, 90.0, 110.0]
    assert window_max_drawdown(navs, 0, 3) == max_drawdown(navs) == pytest.approx(0.25, abs=1e-15)
    assert window_max_drawdown([120.0, 90.0, 110.0], 0, 2) == pytest.approx(0.25, abs=1e-15)
    assert window_max_drawdown([100.0, 110.0, 120.0], 0, 2) == 0.0
    keys = fridays("2015-01-02", 60)
    navs = [100.0] * 60
    navs[52:56] = [120.0, 90.0, 110.0, 110.0]               # weeks 2016-01-01..2016-01-22
    r = rolling_metrics(path(keys, navs), path(keys, navs), (1,))
    w = next(x for x in r.windows if x.window_end == D("2016-01-15"))
    assert w.pre_tax_max_drawdown == pytest.approx(0.25, abs=1e-15)
    assert w.pre_tax_total_return == pytest.approx(0.10, abs=1e-15)


def test_window_drawdown_equals_mdd_definition():
    """The window loop is exactly max_drawdown(navs[i..j]) (same floats) on random paths."""
    rng = random.Random(5)
    navs = [100.0]
    for _ in range(400):
        navs.append(navs[-1] * (1 + rng.gauss(0.001, 0.03)))
    for _ in range(300):
        i = rng.randrange(0, 399)
        j = rng.randrange(i + 1, 401)
        assert window_max_drawdown(navs, i, j) == max_drawdown(navs[i:j + 1])


@pytest.mark.parametrize("years", [1, 3, 5])
def test_constant_growth_gives_constant_rolling_cagr(years):
    """NAV_t = 100 * 1.07 ** (days since inception / 365.2425): every full window has the
    rolling CAGR 7% (MET-003 over its actual elapsed days) and no drawdown."""
    keys = fridays("2001-01-05", 6 * 53)
    inception = keys[0] - WEEK
    navs = [100.0 * 1.07 ** ((k - inception).days / 365.2425) for k in keys]
    r = rolling_metrics(path(keys, navs), path(keys, navs), (years,))
    assert r.windows
    for w in r.windows:
        assert w.pre_tax_cagr == pytest.approx(0.07, rel=1e-9)
        assert w.pre_tax_max_drawdown == 0.0
        assert w.elapsed_days >= int(365.2425 * years) - 1


def test_worst_window_and_tie_break():
    """MET-022: minimum total return, exact; ties -> earlier window_end, then earlier
    window_start; None without a full window of the horizon."""
    def win(start, end, ret):
        return RollingWindow(1, D(start), D(end), 371, 53, 100.0, 100.0 * (1 + ret), ret, None, None,
                             100.0, 100.0 * (1 + ret), ret, None, None)
    ws = [win("2010-01-01", "2011-01-07", -0.2), win("2010-01-08", "2011-01-14", -0.3),
          win("2010-01-15", "2011-01-21", -0.3), win("2009-12-25", "2011-01-21", -0.3)]
    assert worst_rolling_return(ws, 1, "pre_tax") is ws[1]          # earlier window_end
    assert worst_rolling_return(ws[2:], 1, "after_tax") is ws[3]    # same end, earlier start
    assert worst_rolling_return(ws, 3) is None
    keys = fridays("2010-01-01", 120)
    flat = path(keys, [100.0] * 120)
    r = rolling_metrics(flat, flat, (1,))
    assert r.worst(1, "pre_tax") is r.windows[0]                    # all 0.0: first end wins
    with pytest.raises(ValueError):
        worst_rolling_return(ws, 1, "terminal")


def test_insufficient_history_has_no_window():
    """A 5-year path has no 10Y window (no artificial or partial row); worst 10Y is None."""
    keys = fridays("2018-01-05", 5 * 52 + 10)
    p = path(keys, [100.0 + i for i in range(len(keys))])
    r = rolling_metrics(p, p)
    assert r.horizons_years == (1, 3, 5, 10)
    assert r.for_horizon(10) == () and r.worst(10) is None and r.worst(10, "pre_tax") is None
    assert r.for_horizon(5) and r.worst(5) is not None


def test_order_weeks_and_none_profile_paths():
    """Rows sorted by horizon then window_end; weeks = path intervals after the start through
    the end; identical pre-tax and after-tax paths (tax.profile=none) give identical values."""
    keys = fridays("2005-01-07", 4 * 53, skip=("2006-06-09",))
    rng = random.Random(2)
    navs = [100.0]
    for _ in keys:
        navs.append(navs[-1] * (1 + rng.gauss(0.002, 0.02)))
    p = path(keys, navs[1:])
    r = rolling_metrics(p, p, horizons=(3, 1))
    assert r.horizons_years == (1, 3)
    assert [(w.horizon_years, w.window_end) for w in r.windows] == sorted(
        (w.horizon_years, w.window_end) for w in r.windows)
    dates, _ = path_points(p)
    for w in r.windows:
        assert w.weeks == dates.index(w.window_end) - dates.index(w.window_start)
        assert w.elapsed_days == (w.window_end - w.window_start).days
        for f in ("start_nav", "end_nav", "total_return", "cagr", "max_drawdown"):
            assert getattr(w, f"pre_tax_{f}") == getattr(w, f"after_tax_{f}")


def test_pre_and_after_tax_paths_are_independent_columns():
    """Each path gives its own columns; a different after-tax path changes only after_tax_*."""
    keys = fridays("2010-01-01", 120)
    pre = path(keys, [100.0 * 1.002 ** (i + 1) for i in range(120)])
    after = path(keys, [100.0 * 1.001 ** (i + 1) for i in range(120)])
    r = rolling_metrics(pre, after, (1,))
    only_pre = rolling_metrics(pre, pre, (1,))
    for a, b in zip(r.windows, only_pre.windows):
        assert a.pre_tax_total_return == b.pre_tax_total_return
        assert a.after_tax_total_return < a.pre_tax_total_return
    with pytest.raises(ValueError):
        rolling_metrics(pre, path(keys[:-1], [1.0] * 119))


def test_rolling_stats_switch_and_zero_start_nav():
    """metrics.rolling_stats=false keeps total returns and leaves CAGR / drawdown empty; a window
    starting at NAV 0 (a fully distributed foundation path) has no defined return."""
    keys = fridays("2010-01-01", 60)
    p = path(keys, [100.0 + i for i in range(60)])
    r = rolling_metrics(p, p, (1,), stats=False)
    assert all(w.pre_tax_cagr is None and w.after_tax_max_drawdown is None for w in r.windows)
    assert all(w.pre_tax_total_return is not None for w in r.windows)
    zero = path(keys, [0.0] * 60)
    w = rolling_metrics(zero, zero, (1,)).windows[-1]
    assert w.pre_tax_total_return is None and w.pre_tax_cagr is None and w.pre_tax_max_drawdown is None


def test_no_lookahead_future_weeks_do_not_change_past_windows():
    """A window ending at T uses only path points <= T: changing every NAV after T leaves all
    windows ending at or before T identical."""
    keys = fridays("2000-01-07", 12 * 52)
    rng = random.Random(9)
    navs = [100.0]
    for _ in keys:
        navs.append(navs[-1] * (1 + rng.gauss(0.002, 0.025)))
    t = keys[8 * 52]
    changed = [v if k <= t else v * rng.uniform(0.2, 3.0) for k, v in zip(keys, navs[1:])]
    a = rolling_metrics(path(keys, navs[1:]), path(keys, navs[1:]))
    b = rolling_metrics(path(keys, changed), path(keys, changed))
    past = lambda r: [w for w in r.windows if w.window_end <= t]   # noqa: E731
    assert past(a) == past(b) and len(past(a)) > 0
    assert [w for w in a.windows if w.window_end > t] != [w for w in b.windows if w.window_end > t]
