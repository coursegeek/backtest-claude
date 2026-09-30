"""Walk-forward configuration, candidate grid, window construction and signal snapshots without
data (WF-002/003/006..012/015/016, CLI-011 grid, Q-020, Q-022)."""
import copy
import datetime as dt
import math

import pytest

from src import app
from src import walk_forward as wf
from src.cli import resolve
from src.errors import ConfigError, InsufficientHistoryError, NotImplementedCommand
from src.models import RISKY_ASSETS
from src.optimizer import candidate_config
from src.signal_analysis import SignalTrackerSnapshot, reconstruct_tracker

from fixtures.builders import params_for, price_series, random_walk

D = dt.date.fromisoformat
CLI_011 = ["optimize", "--optimization-mode", "walk-forward", "--optimize-params",
           "weights,ma,threshold,delay", "--walk-forward-window", "rolling", "--train-years", "15",
           "--test-years", "5", "--ma-grid", "40,50,60", "--threshold-grid", "0,1,3,5",
           "--delay-grid", "1:4"]


def spec_of(*argv):
    return wf.resolve_walk_forward(resolve(list(argv)))


def fridays(first, last, skip=()):
    out, k = [], D(first)
    while k <= D(last):
        if k not in skip:
            out.append(k)
        k += dt.timedelta(days=7)
    return tuple(out)


# ------------------------------------------------------------------ CLI-011 grid (WF-006..009)
def test_cli_011_grid_is_676_x_3_x_4_x_4():
    spec = spec_of(*CLI_011)
    c = spec.opt.candidates
    assert len(c) == 676 * 3 * 4 * 4 == 32448
    assert all(x.weight_valid for x in c)
    assert spec.parameters == ("weights", "ma", "threshold", "delay")
    assert spec.dimensions == {"ma": (40, 50, 60), "threshold": (0.0, 0.01, 0.03, 0.05),
                               "delay": (1, 2, 3, 4)}
    # weights outer, then ma, threshold, delay (inner); grid_index 1..N
    assert [x.grid_index for x in c[:3]] == [1, 2, 3]
    assert c[0].dims == (("ma", 40), ("threshold", 0.0), ("delay", 1))
    assert c[1].dims == (("ma", 40), ("threshold", 0.0), ("delay", 2))
    assert c[4].dims == (("ma", 40), ("threshold", 0.01), ("delay", 1))
    assert c[48].targets["gold"] == 0.01 and c[47].targets == c[0].targets
    # no other dimension: confirmation, sell_fraction, band stay the configured values
    keys = {k for x in c for k, _ in x.overrides}
    assert keys == {f"signals.{a}.{f}" for a in RISKY_ASSETS
                    for f in ("ma_length", "threshold_off", "threshold_on", "delay_weeks")}
    assert (spec.train_years, spec.test_years, spec.step_years, spec.window_type) == (15, 5, 5.0, "rolling")
    assert spec.opt.union == ("stocks", "gold", "btc")
    # the global warm-up uses the largest grid values (never varies between candidates)
    for a in spec.opt.union:
        p = spec.warmup_params[a]
        assert (p.ma, p.delay) == (60, 4)
        assert p.confirm_off == spec.base.signal_params(a).confirm_off


def test_only_listed_params_vary():
    """WF-006: a parameter outside --optimize-params keeps its configured value; one grid value
    sets the parameter of every active risky asset (Q-022)."""
    spec = spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                   "ma,delay", "--weights", "stocks=0.5,gold=0.3,btc=0.2", "--ma-grid", "10,20",
                   "--delay-grid", "1,3")
    assert len(spec.opt.candidates) == 4 and spec.opt.union == ("stocks", "gold", "btc")
    base = spec.base
    for cand in spec.opt.candidates:
        cfg = candidate_config(base, cand)
        assert cfg.get("allocation.targets") == {"stocks": 0.5, "gold": 0.3, "btc": 0.2, "rf": 0.0}
        d = dict(cand.dims)
        for a in RISKY_ASSETS:
            p, q = cfg.signal_params(a), base.signal_params(a)
            assert (p.ma, p.delay) == (d["ma"], d["delay"])
            assert (p.threshold_off, p.threshold_on, p.confirm_off, p.confirm_on,
                    p.sell_fraction, p.risk_off_action) == (
                q.threshold_off, q.threshold_on, q.confirm_off, q.confirm_on, q.sell_fraction,
                q.risk_off_action)
        assert cfg.get("portfolio.rebalance") == base.get("portfolio.rebalance")
    # grids for parameters that are not optimized are refused, not silently ignored
    with pytest.raises(ConfigError, match="not listed"):
        spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params", "ma",
                "--weights", "stocks=1", "--delay-grid", "1:3")
    with pytest.raises(ConfigError, match="not listed"):
        spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params", "ma",
                "--weights", "stocks=1", "--btc-weight", "0,10")


def test_signal_grids_defaults_units_and_errors():
    """WF-007/008/009 defaults (50, 0.03, 1); WF-010 needs an explicit confirmation grid;
    threshold in CLI percent (Q-026); rebalance_band needs --band-pp; sell_fraction is a decimal
    grid only in walk-forward."""
    s = spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                "ma,threshold,delay", "--weights", "stocks=1")
    assert s.dimensions == {"ma": (50,), "threshold": (0.03,), "delay": (1,)}
    with pytest.raises(ConfigError, match="WF-010"):
        spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                "confirmation", "--weights", "stocks=1")
    s = spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                "confirmation,sell_fraction,rebalance_band", "--weights", "stocks=0.6,gold=0.4",
                "--confirmation-grid", "1,2", "--sell-fraction", "0.25:0.75:0.25", "--band-pp", "2,5")
    assert s.dimensions == {"confirmation": (1, 2), "sell_fraction": (0.25, 0.5, 0.75),
                            "rebalance_band": (2.0, 5.0)}
    assert len(s.opt.candidates) == 12
    cfg = candidate_config(s.base, s.opt.candidates[-1])
    assert cfg.get("portfolio.rebalance") == "band" and cfg.get("portfolio.rebalance_band_pp") == 5.0
    for a in RISKY_ASSETS:
        p = cfg.signal_params(a)
        assert (p.confirm_off, p.confirm_on, p.sell_fraction) == (2, 2, 0.75)
    # sell_fraction listed without a grid: not a dimension, the configured values stay
    s = spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                "sell_fraction,delay", "--weights", "stocks=1")
    assert list(s.dimensions) == ["delay"] and len(s.opt.candidates) == 1
    # outside walk-forward --sell-fraction is one decimal
    assert resolve(["run", "--sell-fraction", "0.3"]).signal_params("gold").sell_fraction == 0.3
    with pytest.raises(ConfigError, match="one decimal fraction"):
        resolve(["run", "--sell-fraction", "0.25,0.5"])
    for argv, msg in (
            (["--optimize-params", "rebalance_band"], "--band-pp"),
            (["--optimize-params", "ma", "--ma-grid", "10,10"], "duplicate"),
            (["--optimize-params", "threshold", "--threshold-grid", "150"], r"\[0, 1\)"),
            (["--optimize-params", "gamma"], "WF-006"),
            (["--optimize-params", "ma,ma"], "WF-006"),
            (["--optimize-params", "delay", "--train-years", "0"], "WF-002/003"),
            (["--optimize-params", "delay", "--step-years", "0"], "WF-012")):
        with pytest.raises(ConfigError, match=msg):
            spec_of("optimize", "--optimization-mode", "walk-forward", "--weights", "stocks=1", *argv)
    with pytest.raises(ConfigError, match="ALLOC-001"):
        spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params", "ma")
    with pytest.raises(ConfigError, match="remove allocation.targets"):
        spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                "weights", "--weights", "stocks=1")


def test_modes_and_dispatch(monkeypatch):
    """WF-001: in-sample stays the in-sample optimizer; walk-forward is dispatched to
    walk_forward.run_walk_forward (validation before any data)."""
    calls = []
    monkeypatch.setattr(wf, "run_walk_forward", lambda cfg: calls.append(cfg.get("optimizer.mode")))
    app.dispatch(resolve(["optimize", "--optimization-mode", "walk-forward", "--weights", "stocks=1",
                          "--optimize-params", "ma"]))
    assert calls == ["walk-forward"]
    from src import optimizer as opt
    with pytest.raises(ConfigError, match="resolve_walk_forward"):
        opt.resolve_optimizer(resolve(["optimize", "--optimization-mode", "walk-forward"]))
    with pytest.raises(NotImplementedCommand, match="walk-forward"):
        opt.resolve_optimizer(resolve(["optimize", "--optimize-params", "weights,ma"]))
    s = spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params", "delay",
                "--weights", "stocks=1", "--walk-forward-window", "anchored")
    assert s.window_type == "anchored" and s.base.get("report.run_name") == "walk_forward"
    with pytest.raises(ConfigError):
        resolve(["optimize", "--walk-forward-window", "sliding"])


# ------------------------------------------------------------------ windows (WF-002/003/011/012/015/016)
def test_window_construction_rolling_train_15_test_5():
    """TEST_PLAN TEST-049 shape: 23 years, train 15, test 5 -> OOS 15..20 and 20..23."""
    weeks = fridays("1990-01-05", "2012-12-28")
    ws = wf.plan_windows(weeks, "rolling", 15, 5, wf.step_rule(5))
    assert len(ws) == 2
    a, b = ws
    assert (a.anchor, a.train_start, a.train_end, a.test_start) == (
        D("2005-01-05"), D("1990-01-05"), D("2004-12-31"), D("2005-01-07"))
    assert (a.nominal_test_end, a.actual_oos_end) == (D("2010-01-01"), D("2010-01-01"))
    assert (b.anchor, b.train_start, b.train_end, b.test_start) == (
        D("2010-01-05"), D("1995-01-06"), D("2010-01-01"), D("2010-01-08"))
    assert b.nominal_test_end == D("2015-01-02") and b.actual_oos_end == D("2012-12-28")   # WF-015
    assert b.actual_test_days == (D("2012-12-28") - D("2010-01-08")).days + 1 == 1086
    assert b.actual_test_years == 1086 / 365.2425
    assert a.actual_test_days == 1821 and a.train_weeks == 783
    # OOS segments are contiguous, disjoint and cover every week from the first test start
    oos = [w for x in ws for w in x.oos_weeks]
    assert oos == [w for w in weeks if w >= a.test_start]
    for x in ws:
        assert x.train_end < x.test_start and x.train_end == weeks[weeks.index(x.test_start) - 1]


def test_window_anchored_and_insufficient_history():
    weeks = fridays("1990-01-05", "2012-12-28")
    r = wf.plan_windows(weeks, "rolling", 10, 5, wf.step_rule(5))
    an = wf.plan_windows(weeks, "anchored", 10, 5, wf.step_rule(5))
    assert [x.test_start for x in r] == [x.test_start for x in an]
    assert [x.train_end for x in r] == [x.train_end for x in an]
    assert {x.train_start for x in an} == {weeks[0]}                       # WF-011 anchored
    assert [x.train_start for x in r] == [D("1990-01-05"), D("1995-01-06"), D("2000-01-07")]
    assert [x.train_weeks for x in an] == sorted(x.train_weeks for x in an)
    with pytest.raises(InsufficientHistoryError,
                       match="insufficient history for requested walk-forward training window"):
        wf.plan_windows(fridays("2011-01-07", "2026-07-31"), "rolling", 16, 5, wf.step_rule(5))
    # the training window is never shortened: exactly train_years of history is enough
    ws = wf.plan_windows(fridays("2011-01-07", "2026-07-31"), "rolling", 15, 5, wf.step_rule(5))
    assert len(ws) == 1 and ws[0].test_start == D("2026-01-09") and ws[0].actual_oos_end == D("2026-07-31")


def test_step_semantics_integer_fractional_and_longer_than_nominal():
    """WF-012 (Q-022): step_years is the reoptimization cadence; the OOS segment runs to the
    week before the next test start, the last one to the global end (WF-015)."""
    weeks = fridays("1990-01-05", "2012-12-28")
    two = wf.plan_windows(weeks, "rolling", 15, 5, wf.step_rule(2))
    assert [x.test_start for x in two] == [D("2005-01-07"), D("2007-01-05"), D("2009-01-09"),
                                           D("2011-01-07")]
    assert all(x.nominal_test_end > x.actual_oos_end for x in two)        # shorter than nominal
    assert two[-1].actual_oos_end == weeks[-1]
    long_ = wf.plan_windows(weeks, "rolling", 15, 1, wf.step_rule(3))
    assert [x.test_start for x in long_] == [D("2005-01-07"), D("2008-01-11"), D("2011-01-07")]
    first = long_[0]
    assert first.nominal_test_end == D("2005-12-30") and first.actual_oos_end == D("2008-01-04")
    assert first.actual_test_days > 365                                    # longer than nominal
    assert wf.step_rule(1.5) == ("days", 548)                               # 547.86375 -> 548
    assert wf.step_rule(2.0) == ("calendar_years", 2)
    assert wf.step_rule(0.5) == ("days", 183)                               # 182.62125 -> 183
    half = wf.plan_windows(weeks, "rolling", 15, 5, wf.step_rule(1.5))
    assert [x.anchor for x in half[:3]] == [D("2005-01-05"), D("2005-01-05") + dt.timedelta(548),
                                            D("2005-01-05") + dt.timedelta(1096)]
    assert [x.test_start for x in half[:2]] == [D("2005-01-07"), D("2006-07-07")]


def test_windows_on_calendar_with_removed_weeks_and_leap_day():
    gone = {D("2005-01-07"), D("2005-01-14")}
    weeks = fridays("1990-01-05", "2012-12-28", skip=gone)
    a = wf.plan_windows(weeks, "rolling", 15, 5, wf.step_rule(5))[0]
    assert a.test_start == D("2005-01-21") and a.train_end == D("2004-12-31")
    assert wf.add_years(D("2004-02-29"), 1) == D("2005-02-28")
    assert wf.add_years(D("2004-02-29"), 4) == D("2008-02-29")


# ------------------------------------------------------------------ signal snapshots and the memo
def test_snapshot_is_immutable_and_independent_of_the_live_tracker():
    """Q-022 point 25/70: a SignalTrackerSnapshot shares nothing with a live tracker; mutating
    a restored or reconstructed tracker never changes the snapshot or the reconstruction memo."""
    series = price_series(random_walk(120, 5), role="stocks_price")
    p = params_for("stocks", ma=5, confirm_off=2, confirm_on=2, delay=3)
    start = series.points[80].week_key
    tr, _ = reconstruct_tracker(series, p, start)
    snap = SignalTrackerSnapshot.of(tr)
    frozen = copy.deepcopy(snap)
    live = snap.restore()
    for pt in series.points[80:100]:                  # the live OOS tracker moves on
        live.due(pt.week_key)
        live.observe(pt)
    assert snap == frozen
    again, _ = reconstruct_tracker(series, p, start)  # memo entry unchanged by the mutations
    assert SignalTrackerSnapshot.of(again) == frozen
    tr.observe(series.points[80])                     # mutate the memo's returned copy
    third, _ = reconstruct_tracker(series, p, start)
    assert SignalTrackerSnapshot.of(third) == frozen
    other = snap.restore()
    assert other is not live and list(other.prices) == list(frozen.window_prices)
    assert other.queue.pending == frozen.pending
    assert snap.same_state(SignalTrackerSnapshot.of(other))
    with pytest.raises(dataclasses_frozen()):
        snap.gaps = 3


def dataclasses_frozen():
    import dataclasses
    return dataclasses.FrozenInstanceError


def test_grid_neighbours():
    spec = spec_of("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
                   "weights,ma", "--btc-weight", "0,10,20", "--gold-weight", "0,10", "--ma-grid",
                   "10,20")
    coords = spec.coordinates
    assert len(coords) == 12
    sel = next(c.grid_index for c in spec.opt.candidates
               if c.targets["btc"] == 0.1 and c.targets["gold"] == 0.0 and dict(c.dims)["ma"] == 10)
    near = wf.neighbours(coords, sel)
    got = sorted((spec.opt.candidates[g - 1].targets["btc"], spec.opt.candidates[g - 1].targets["gold"],
                  dict(spec.opt.candidates[g - 1].dims)["ma"]) for g in near)
    assert got == [(0.0, 0.0, 10), (0.1, 0.0, 20), (0.1, 0.1, 10), (0.2, 0.0, 10)]
    assert math.isclose(sum(1 for _ in near), 4)
