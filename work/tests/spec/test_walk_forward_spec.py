"""Walk-forward specification tests TEST-021, TEST-037 and TEST-049 (WF-004, WF-013..016,
META-003, Q-020, Q-022) on synthetic histories written in the raw source formats."""
import csv
import dataclasses
import datetime as dt
import json

import pytest

from src import walk_forward as wf
from src.models import TradeReason

from fixtures.market import (engineered, market_returns, oos_path_for, signal_overrides,
                             wf_config, write_market)

D = dt.date.fromisoformat


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ============================================================================ TEST-021
FIRST, N = "1996-01-05", 470                       # 1996-01-05 .. 2004-12-31
GRID = ["--optimize-params", "weights,ma,threshold,delay", "--btc-weight", "0",
        "--gold-weight", "0,30", "--ma-grid", "8,20", "--threshold-grid", "0,2",
        "--delay-grid", "1,3", "--train-years", "3", "--test-years", "1", "--jobs", "1"]


def run_on(tmp_path, name, returns):
    files = write_market(tmp_path / name, FIRST, returns)
    return wf.run_walk_forward(wf_config(files, *GRID), write=False)


def changed_from(returns, start):
    """Drastically different data from ``start`` on (sign flipped and tripled)."""
    keys = [D(FIRST) + i * dt.timedelta(days=7) for i in range(N)]
    return {a: [(-3.0 * r if k >= start else r) for k, r in zip(keys, pct)]
            for a, pct in returns.items()}


def selection_fields(sel):
    p = sel.params
    return (sel.grid_index, sel.targets, sel.objective_value,
            {a: (x.ma, x.threshold_off, x.threshold_on, x.delay, x.confirm_off, x.confirm_on)
             for a, x in p.items()}, sel.training_input_sha256, sel.max_training_week)


def test_no_future_data_in_training(tmp_path):
    """TEST-021 / WF-004 / WF-014 / META-003: the selection of a window depends only on data
    before its test start. Rewriting every source from the first test start on leaves the
    first selection (grid index, weights, MA, threshold, delay, confirmation, training
    objective, the whole training grid) unchanged; rewriting from the second test start leaves
    the first two selections and the whole first OOS segment (trades, NAV) unchanged."""
    base_returns = market_returns(FIRST, N, seed=21, assets=("stocks", "gold"))
    a = run_on(tmp_path, "a", base_returns)
    assert len(a.windows) >= 4
    t1, t2 = a.windows[0].test_start, a.windows[1].test_start
    b = run_on(tmp_path, "b", changed_from(base_returns, t1))
    c = run_on(tmp_path, "c", changed_from(base_returns, t2))
    assert [w.test_start for w in a.windows] == [w.test_start for w in b.windows] \
        == [w.test_start for w in c.windows]
    # the rewritten data really changes later decisions / results
    assert a.path.engine.weeks[-1].nav_end != b.path.engine.weeks[-1].nav_end
    assert [s.grid_index for s in a.selections] != [s.grid_index for s in b.selections] or \
        [s.objective_value for s in a.selections[1:]] != [s.objective_value for s in b.selections[1:]]
    # window 1: identical selection and identical full training grid in a and b
    assert selection_fields(a.selections[0]) == selection_fields(b.selections[0])
    assert a.training[0].rows == b.training[0].rows
    # windows 1 and 2 identical in a and c; the whole first OOS segment identical
    for i in (0, 1):
        assert selection_fields(a.selections[i]) == selection_fields(c.selections[i])
        assert a.training[i].rows == c.training[i].rows
    s_a, s_c = a.path.segments[0].engine, c.path.segments[0].engine
    assert [dataclasses.astuple(t) for t in s_a.trades] == [dataclasses.astuple(t) for t in s_c.trades]
    assert [(w.week_key, w.nav_end) for w in s_a.weeks] == [(w.week_key, w.nav_end) for w in s_c.weeks]
    assert s_a.final_snapshot == s_c.final_snapshot
    # the training views physically hold nothing from the test window or later
    for res in (a, b, c):
        assert res.manifest["no_future_training_checks_passed"] is True
        for w, sel in zip(res.windows, res.selections):
            assert sel.max_training_week < w.test_start
            view = res.prepared.training_view(w.train_start, w.train_end, w.test_start)
            assert max(view.inputs.weeks) < w.test_start and max(view.inputs.market) < w.test_start
            assert all(p.available_at < w.test_start for s in view.inputs.signal_series.values()
                       for p in s.points)
            assert view.cpi_series is None and view.max_information_week() < w.test_start


# ============================================================================ TEST-037
def continuity_scenario(tmp_path, profile="individual_pl"):
    """Stocks only, SMA 3, confirmation 1, delay 1, sell 50 %; train 2 / test 1 year from
    1999-12-31: OOS windows 2002, 2003, 2004 H1. Window 1: an exit at a loss (lot 1 partly
    sold) and a re-entry (lot 2), the position stays open, the 2002 loss is still in the open
    tax year. Window 2: the 2002 year is closed in its first week (loss bucket), an exit sells
    the rest of lot 1 at its window-1 cost basis with a gain."""
    stocks = engineered("1999-01-01", 287, 0.3, [
        ("2002-03-01", "2002-03-15", -4.0), ("2002-04-05", "2002-04-19", 3.0),
        ("2002-04-26", "2002-12-27", 0.5), ("2003-06-06", "2003-06-13", -5.0),
        ("2003-07-04", "2003-07-18", 4.0)])
    files = write_market(tmp_path / "m", "1999-01-01", {"stocks": stocks})
    cfg = wf_config(files, "--optimize-params", "delay", "--delay-grid", "1", "--weights",
                    "stocks=1", "--train-years", "2", "--test-years", "1", "--start", "1999-12-31",
                    "--tax-profile", profile, "--dividend-tax-mode", "off", "--jobs", "1",
                    overrides=signal_overrides(("stocks",), ma_length=3, confirm_off_weeks=1,
                                               confirm_on_weeks=1, sell_fraction=0.5,
                                               threshold_off=0.0, threshold_on=0.0))
    return cfg


def test_state_continuity_between_windows(tmp_path):
    """TEST-037 / WF-004 / WF-013: NAV, holdings, lots (ids, units, cost basis), unit prices,
    realizations, loss buckets and the open tax year continue across OOS boundaries; the
    initial capital and allocation are not reset; a sale in window 2 uses window 1's lots."""
    cfg = continuity_scenario(tmp_path)
    res = wf.run_walk_forward(cfg, write=False)
    w1, w2, w3 = res.windows
    assert (w1.test_start, w2.test_start, w3.test_start) == (D("2002-01-04"), D("2003-01-03"),
                                                             D("2004-01-02"))
    s1, s2, s3 = res.path.segments
    e1, e2 = s1.engine, s2.engine
    # window 1: >= 2 open lots, a realized loss, the position stays open, open tax year 2002
    lots1 = dict(s1.state.portfolio.lots)["stocks"]
    assert len(lots1) >= 2 and s1.state.portfolio.ledger.stocks > 0
    assert any(r.realized_gain < 0 for r in e1.realizations)
    ts1 = s1.state.tax_state
    assert ts1.open_year == 2002 and ts1.realized_gain_by_year()[2002] < 0 and not ts1.loss_buckets
    # boundary: no reset of NAV, holdings, lots, unit prices, costs
    assert e2.initial_ledger == e1.final_ledger == s1.state.portfolio.ledger
    assert e2.weeks[0].nav_start == e1.weeks[-1].nav_end
    assert e2.weeks[0].nav_start != float(cfg.get("portfolio.initial_capital_pln"))
    first2 = e2.weeks[0]
    assert first2.trades == () and first2.rebalance is None           # no re-allocation
    snap = s1.state.portfolio
    assert (snap.lots, snap.next_lot_id, snap.unit_prices) == (e1.final_snapshot.lots,
                                                               e1.final_snapshot.next_lot_id,
                                                               e1.final_snapshot.unit_prices)
    # window 2, first week (2003-01-03) step 2: the carried 2002 year is closed -> loss bucket
    ts2 = s2.state.tax_state
    liab = ts2.annual_liabilities[2002]
    assert liab.determined_week == D("2003-01-03") and liab.annual_realized == ts1.realized_gain_by_year()[2002]
    assert [b.year for b in ts2.loss_buckets][:1] == [2002]
    assert ts2.loss_buckets[0].original == -ts1.realized_gain_by_year()[2002]
    ev = [e for e in ts2.tax_events if e.week_key == D("2003-01-03")]
    assert ev and all(e.pipeline_step == 2 for e in ev if e.settlement == "annual")
    # window 2 sale: consumes window-1 lots with their true cost basis (not a NAV restatement)
    sale = [r for r in e2.realizations]
    assert sale and sale[0].realized_gain > 0
    by_id = {lot.lot_id: lot for lot in lots1}
    for lot_id, units, cost in sale[0].consumed:
        assert lot_id in by_id
        assert cost == pytest.approx(by_id[lot_id].cost * units / by_id[lot_id].units, rel=1e-12)
    assert sale[0].consumed[0][0] == min(by_id)                        # FIFO: the oldest lot
    # window 3, first week: the 2003 gain uses the carried 2002 loss bucket
    ts3 = s3.state.tax_state
    l03 = ts3.annual_liabilities[2003]
    assert l03.determined_week == D("2004-01-02") and l03.annual_realized > 0
    assert dict(l03.buckets_used).get(2002, 0) > 0 and l03.loss_offset > 0
    # one initial allocation only; terminal settlement only after the last OOS week
    assert sum(1 for s in res.path.segments if s.plan["reasons"] == ("initial_allocation",)) == 1
    assert all(t.phase == "weekly" and t.reason not in (TradeReason.TERMINAL_LIQUIDATION,)
               for t in res.path.engine.trades)
    assert res.portfolio.terminal.liquidation_trades and \
        res.portfolio.terminal.liquidation_trades[0].week_key == w3.actual_oos_end
    # the stitched journals contain every record exactly once
    assert len(res.path.engine.trades) == len(e1.trades) + len(e2.trades) + len(s3.engine.trades)
    assert [w.week_key for w in res.path.engine.weeks] == [w for x in res.windows for w in x.oos_weeks]


# ============================================================================ TEST-049
LONG_FIRST, LONG_N = "1989-01-06", 1200            # 1989-01-06 .. 2011-12-30 (23 years)


def test_partial_last_window_reported(tmp_path):
    """TEST-049 / WF-015 / WF-016: train 15, test 5 on 23 years of history -> OOS 15..20 and
    20..23; the shorter last window is kept and reported with its actual length; the stitched
    OOS path ends at the global end. With an explicit step longer than test_years an OOS
    segment is longer than the nominal test window (reoptimization cadence, Q-022)."""
    files = write_market(tmp_path / "m", LONG_FIRST,
                         market_returns(LONG_FIRST, LONG_N, seed=49, assets=("stocks",)))
    args = ["--optimize-params", "delay", "--delay-grid", "1", "--weights", "stocks=1",
            "--train-years", "15", "--jobs", "1", "--output-dir", str(tmp_path / "out")]
    res = wf.run_walk_forward(wf_config(files, *args, "--test-years", "5"))
    rows = read_csv(res.output_dir / "walk_forward_results.csv")
    assert len(rows) == 2
    first_week = res.prepared.inputs.weeks[0]
    end = res.prepared.inputs.weeks[-1]
    assert end == D("2011-12-30")
    a, b = rows
    assert D(a["test_start"]) == min(w for w in res.prepared.inputs.weeks
                                     if w >= wf.add_years(first_week, 15))
    assert D(b["test_start"]) == min(w for w in res.prepared.inputs.weeks
                                     if w >= wf.add_years(first_week, 20))
    assert D(b["actual_oos_end"]) == end and D(b["nominal_test_end"]) > end    # partial, kept
    days = (end - D(b["test_start"])).days + 1
    assert int(b["actual_test_days"]) == days and float(b["actual_test_years"]) == days / 365.2425
    assert float(b["actual_test_years"]) < 5 < float(a["actual_test_years"]) + 0.1
    assert D(a["actual_oos_end"]) == D(b["test_start"]) - dt.timedelta(days=7)
    summary = read_csv(res.output_dir / "summary.csv")[0]
    assert (summary["oos_windows"], summary["last_oos_week"], summary["first_oos_week"]) == (
        "2", end.isoformat(), a["test_start"])
    weekly = read_csv(res.output_dir / "weekly_portfolio.csv")
    assert weekly[0]["week_key"] == a["test_start"] and weekly[-1]["week_key"] == end.isoformat()
    # step 3 years with test_years 1: segments follow the reoptimization cadence
    res = wf.run_walk_forward(wf_config(files, *args, "--test-years", "1", "--step-years", "3"),
                              write=False)
    rows = res.results_rows
    assert len(rows) == 3
    assert rows[0]["actual_test_days"] > 366 and rows[0]["nominal_test_end"] < rows[0]["actual_oos_end"]
    assert rows[-1]["actual_oos_end"] == end
    for r in rows:
        assert r["actual_test_days"] == (r["actual_oos_end"] - r["test_start"]).days + 1
        assert r["actual_test_years"] == r["actual_test_days"] / 365.2425
    m = res.manifest
    assert [w["actual_test_days"] for w in m["windows"]] == [r["actual_test_days"] for r in rows]
    assert json.dumps(m["step_rule"]) and m["step_years"] == 3.0
