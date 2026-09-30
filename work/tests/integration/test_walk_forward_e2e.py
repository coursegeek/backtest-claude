"""Walk-forward end to end on synthetic histories (WF-001..016, REP-016, CLI-011, Q-020, Q-022):
OOS continuity against one continuous run, boundary rebalancing, signal-state handover,
pending executions, foundation state, outputs, serial vs parallel, CLI."""
import csv
import dataclasses
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import app
from src import walk_forward as wf
from src.cli import resolve
from src.models import State, TradeReason
from src.reporting import SUMMARY_FIELDS, summary_row

from fixtures.market import (engineered, market_returns, oos_path_for, signal_overrides,
                             wf_config, write_market)

WORK = Path(__file__).resolve().parents[2]
BT = str(WORK / "backtest.py")
D = dt.date.fromisoformat
FIRST, N = "1999-01-01", 287                        # 1999-01-01 .. 2004-06-25
FAST = dict(ma_length=3, confirm_off_weeks=1, confirm_on_weeks=1, sell_fraction=0.5,
            threshold_off=0.0, threshold_on=0.0)
WINDOWS = ["--train-years", "2", "--test-years", "1", "--start", "1999-12-31", "--jobs", "1"]


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def astuples(xs):
    return [dataclasses.astuple(x) for x in xs]


# ============================================================================ continuity
WF_ONLY = {"run_name", "optimization_mode", "walk_forward_window", "train_years", "test_years",
           "step_years", "oos_windows", "first_oos_week", "last_oos_week", "requested_start",
           "requested_end"}


@pytest.mark.parametrize("profile,rebalance", [
    ("none", ["--rebalance", "monthly"]),
    ("individual_pl", ["--rebalance", "band", "--rebalance-band-pp", "2"]),
    ("family_foundation_19", ["--rebalance", "quarterly"]),
    ("family_foundation_15", ["--rebalance", "band", "--rebalance-band-pp", "3"])])
def test_constant_selection_equals_one_continuous_run(tmp_path, profile, rebalance):
    """WF-004/WF-013/Q-022: with the same selection in every window the stitched OOS path is
    exactly one continuous run from the first test start - NAV, trades, payments, transfers,
    rebalancing (incl. a carried band trigger), tax / foundation state, annual taxes and admin
    costs determined in the first week of a new window (New Year boundaries), terminal
    settlement once, pre-tax shadow and every summary metric."""
    def jumps(keys, pct):                  # BTC +80 % in the last week of windows 1 and 2
        return [80.0 if k in (D("2002-12-27"), D("2003-12-26")) else r for k, r in zip(keys, pct)]

    files = write_market(tmp_path / "m", FIRST, market_returns(FIRST, N, seed=3,
                                                               overrides={"btc": jumps}))
    argv = ["--weights", "stocks=0.5,gold=0.3,btc=0.2", "--tax-profile", profile,
            "--dividend-tax-mode", "off", "--transaction-cost-bps", "10", "--slippage-bps", "5",
            *rebalance]
    cfg = wf_config(files, "--optimize-params", "delay", "--delay-grid", "1", *WINDOWS, *argv,
                    overrides=signal_overrides(**FAST))
    res = wf.run_walk_forward(cfg, write=False)
    ts = [w.test_start for w in res.windows]
    assert ts == [D("2002-01-04"), D("2003-01-03"), D("2004-01-02")]      # New Year boundaries
    run_cfg = resolve(["run", "--start", ts[0].isoformat(), "--as-of-date", "2026-09-29", *files,
                       *argv]).with_overrides(signal_overrides(**FAST))
    one = app.run_portfolio(run_cfg, write=False)
    a, b = res.portfolio, one
    assert [(w.week_key, w.ledger_end) for w in a.engine.weeks] == \
        [(w.week_key, w.ledger_end) for w in b.engine.weeks]
    for name in ("trades", "payments", "transfers", "rebalance_events", "realizations",
                 "signal_records"):
        assert astuples(getattr(a.engine, name)) == astuples(getattr(b.engine, name)), name
    assert a.engine.final_snapshot == b.engine.final_snapshot
    if profile != "none":
        assert astuples(a.tax_state.tax_events) == astuples(b.tax_state.tax_events)
        assert a.terminal.after_tax_terminal_wealth == b.terminal.after_tax_terminal_wealth
        assert astuples(a.terminal.terminal_tax_events) == astuples(b.terminal.terminal_tax_events)
        assert [w.ledger_end for w in a.pre_tax.engine.weeks] == [w.ledger_end for w in b.pre_tax.engine.weeks]
    assert a.pre_tax.final_wealth == b.pre_tax.final_wealth
    ra, rb = summary_row(res.spec.base, a), summary_row(run_cfg, b)
    assert {k for k in SUMMARY_FIELDS if ra[k] != rb[k]} <= WF_ONLY
    assert ra["optimization_mode"] == "walk-forward" and ra["oos_windows"] == 3
    if "band" in rebalance:                # the band trigger of 2002-12-27 crosses the boundary
        assert [s.plan["pending_action"] for s in res.path.segments] == ["none", "carried", "carried"]
        ev = [e for e in a.engine.rebalance_events if e.week_key in (ts[1], ts[2])]
        assert [(e.reason, e.trigger_source_week, e.week_key) for e in ev] == [
            (TradeReason.BAND_REBALANCE, D("2002-12-27"), ts[1]),
            (TradeReason.BAND_REBALANCE, D("2003-12-26"), ts[2])]
    if profile.startswith("family_foundation"):
        st = a.tax_state
        setup = [e for e in st.tax_events if e.event_type == "foundation_setup_cost"]
        assert len(setup) == 1 and setup[0].week_key == D("2001-12-28")     # once, at inception
        admin = [(e.tax_year, e.week_key) for e in st.tax_events if e.category == "cost"
                 and e.settlement == "annual"]
        # prorated 2001 (inception 2001-12-28, Q-034), then 2002 and 2003: each determined in
        # step 2 of the first week of a window (New Year boundaries), never twice or skipped
        assert admin == [(2001, D("2002-01-04")), (2002, D("2003-01-03")), (2003, D("2004-01-02"))]
        assert not [e for e in st.tax_events if e.settlement == "terminal"]
        assert [e.tax_year for e in a.terminal.terminal_tax_events if e.category == "cost"] == [2004]


def test_no_terminal_settlement_at_internal_boundaries(tmp_path):
    """Q-022 point 43/61: no terminal liquidation, foundation distribution, terminal capital
    gains or distribution tax at an internal boundary - only after the last OOS week."""
    files = write_market(tmp_path / "m", FIRST, market_returns(FIRST, N, seed=11))
    for profile in ("individual_pl", "family_foundation_19"):
        cfg = wf_config(files, "--optimize-params", "weights", "--btc-weight", "0,20",
                        "--gold-weight", "0,20", *WINDOWS, "--tax-profile", profile,
                        "--dividend-tax-mode", "off", overrides=signal_overrides(**FAST))
        res = wf.run_walk_forward(cfg, write=False)
        terminal = {TradeReason.TERMINAL_LIQUIDATION, TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION}
        for seg in res.path.segments + res.path.shadow_segments:
            assert not [t for t in seg.engine.trades if t.reason in terminal or t.phase != "weekly"]
            assert not [p for p in seg.engine.payments if p.phase != "weekly"]
            st = seg.state.tax_state
            assert not [e for e in st.tax_events if e.phase == "terminal" or e.settlement == "terminal"]
            assert st.open_year == seg.window.actual_oos_end.year        # final year still open
        t = res.portfolio.terminal
        assert t.liquidation_trades and {x.week_key for x in t.liquidation_trades} == {
            res.windows[-1].actual_oos_end}
        assert {x.reason for x in t.liquidation_trades} <= terminal


# ============================================================================ boundaries
def boundary_data(tmp_path, stocks=None):
    ret = {"stocks": stocks or engineered(FIRST, N, 0.3, [
                ("2002-01-04", "2002-08-30", 0.6), ("2002-09-06", "2002-09-13", -5.0),
                ("2002-10-04", "2002-10-18", 3.0)]),
           "gold": engineered(FIRST, N, 0.2, [("2003-12-05", "2003-12-19", -5.0)]),
           "btc": engineered(FIRST, N, 0.5)}
    return write_market(tmp_path / "m", FIRST, ret)


def test_weight_change_boundary_rebalance(tmp_path):
    """WF-013 / Q-022 points 35-41: window 1 stocks 80 / gold 20, window 2 stocks 40 / gold 40
    / btc 20, window 3 stocks 100. In the first week of windows 2 and 3 a walk_forward_rebalance
    in step 3 (after the signal step, together with the annual tax due, TAX-007) realigns the
    portfolio: stocks sold with a realization on window-1 lots, BTC bought (new lot, costs),
    gold/BTC and the gold RF reserve fully liquidated when they leave; the first week's return
    is earned on the new weights."""
    files = boundary_data(tmp_path)
    cfg = wf_config(files, "--optimize-params", "delay", "--delay-grid", "1", "--weights",
                    "stocks=0.8,gold=0.2", *WINDOWS, "--tax-profile", "individual_pl",
                    "--dividend-tax-mode", "off", "--transaction-cost-bps", "10",
                    "--slippage-bps", "5", overrides=signal_overrides(**FAST))
    targets = ({"stocks": 0.8, "gold": 0.2}, {"stocks": 0.4, "gold": 0.4, "btc": 0.2},
               {"stocks": 1.0})
    cfgs = [cfg.with_overrides({"allocation.targets": t}) for t in targets]
    spec, prepared, windows, sels, path = oos_path_for(cfg, cfgs, assets=("stocks", "gold", "btc"))
    s1, s2, s3 = path.segments
    # ---- window 2: weights and active assets change
    assert s2.plan["force"] and set(s2.plan["reasons"]) == {"strategic_weights_changed",
                                                             "active_assets_changed"}
    assert s2.plan["carried"] == ("stocks", "gold") and s2.plan["replaced"] == ("btc",)
    w0 = s2.engine.weeks[0]
    assert w0.week_key == D("2003-01-03") and w0.amounts_due.total > 0       # 2002 CGT due now
    assert [p.pipeline_step for p in w0.payments] == [3] and \
        w0.payments[0].amount == w0.amounts_due.total
    assert w0.rebalance.reason == TradeReason.WALK_FORWARD_REBALANCE
    assert w0.rebalance.amounts_due == w0.amounts_due.total                   # TAX-007 in one plan
    assert {t.reason for t in w0.trades} == {TradeReason.WALK_FORWARD_REBALANCE}
    assert all(t.pipeline_step == 3 for t in w0.trades)
    by = {(t.asset, t.side): t for t in w0.trades}
    assert set(by) == {("stocks", "sell"), ("gold", "buy"), ("btc", "buy")}
    btc = by[("btc", "buy")]
    assert btc.transaction_cost > 0 and btc.slippage > 0
    new_lot = [l for a, lots in s2.engine.final_snapshot.lots if a == "btc" for l in lots]
    assert new_lot[0].open_week == D("2003-01-03") and new_lot[0].source == "buy"
    sale = [r for r in s2.engine.realizations if r.week_key == D("2003-01-03")]
    lots1 = {l.lot_id: l for a, lots in s1.state.portfolio.lots if a == "stocks" for l in lots}
    assert sale and sale[0].asset == "stocks" and sale[0].realized_gain > 0
    for lot_id, units, cost in sale[0].consumed:
        assert cost == pytest.approx(lots1[lot_id].cost * units / lots1[lot_id].units, rel=1e-12)
    assert s2.state.tax_state.realizations[2003][0][1] == sale[0].realized_gain
    after3 = w0.ledger_before_returns.sleeve_weights()
    for k, v in {"stocks": 0.4, "gold": 0.4, "btc": 0.2, "rf": 0.0}.items():
        assert after3[k] == pytest.approx(v, abs=1e-12)
    for a in ("stocks", "gold", "btc"):                              # step 4 on the new holdings
        assert w0.ledger_after_returns.asset(a) == pytest.approx(
            w0.ledger_before_returns.asset(a) * (1 + w0.market.asset_returns[a]), rel=1e-12)
    # ---- window 3: gold (RISK_OFF, with an RF reserve) and BTC leave the portfolio
    assert set(s3.plan["reasons"]) >= {"strategic_weights_changed", "active_assets_changed"}
    assert s2.state.portfolio.ledger.rf_reserve_gold > 0             # gold was RISK_OFF
    assert s2.state.signal_states["gold"].effective_state == State.RISK_OFF
    w0 = s3.engine.weeks[0]
    sells = {t.asset for t in w0.trades if t.side == "sell"}
    assert sells == {"gold", "btc"} and {t.asset for t in w0.trades if t.side == "buy"} == {"stocks"}
    end3 = w0.ledger_before_returns
    assert end3.gold == end3.btc == end3.rf_reserve_gold == end3.rf_reserve_btc == 0.0
    assert end3.stocks / end3.nav == pytest.approx(1.0, abs=1e-12)  # no orphan reserve
    realized = {r.asset: r.realized_gain for r in s3.engine.realizations if r.week_key == w0.week_key}
    assert set(realized) == {"gold", "btc"}
    assert s3.state.tax_state.realizations[2004][:2] == [
        (a, g, w0.week_key) for a, g in [(r.asset, r.realized_gain) for r in s3.engine.realizations
                                         if r.week_key == w0.week_key]]


def pending_data(tmp_path):
    """Stocks +0.3 %/week with a fall in the last week of window 1 (2002-12-27, -6 %) and two
    more weak weeks (-1 %) at the start of window 2."""
    stocks = engineered(FIRST, N, 0.3, [("2002-12-27", "2002-12-27", -6.0),
                                        ("2003-01-03", "2003-01-10", -1.0)])
    return write_market(tmp_path / "m", FIRST, {"stocks": stocks})


def stocks_cfg(files, **sig):
    return wf_config(files, "--optimize-params", "delay", "--delay-grid", "1", "--weights",
                     "stocks=1", *WINDOWS, overrides=signal_overrides(("stocks",), **dict(FAST, **sig)))


def test_signal_param_change_cancels_old_pending(tmp_path):
    """Q-022 points 31/32/57: window 1 MA 4 / delay 3 leaves a pending exit (confirmed
    2002-12-27, due 2003-01-17); window 2 selects MA 6 / delay 1: the old pending execution is
    cancelled and never executed, the tracker is the selected training state (history before
    the test start only) and its pending exit (due 2003-01-03) runs in step 1 of the first
    week."""
    files = pending_data(tmp_path)
    c1 = stocks_cfg(files, ma_length=4, delay_weeks=3)
    c2 = stocks_cfg(files, ma_length=6, delay_weeks=1)
    spec, prepared, windows, sels, path = oos_path_for(c1, [c1, c2, c2])
    s1, s2 = path.segments[:2]
    old = s1.state.signal_states["stocks"].pending
    assert [(e.confirm_week, e.execution_week) for e in old] == [(D("2002-12-27"), D("2003-01-17"))]
    plan = s2.plan
    assert plan["replaced"] == ("stocks",) and plan["carried"] == ()
    assert plan["cancelled"] == old
    assert [(e.confirm_week, e.execution_week) for e in plan["adopted"]] == [
        (D("2002-12-27"), D("2003-01-03"))]
    snap = plan["signal_states"]["stocks"]
    assert snap is sels[1].signal_states["stocks"]
    assert snap.last_key == windows[1].train_end == D("2002-12-27") < windows[1].test_start
    assert (snap.params.ma, snap.params.delay) == (6, 1)
    first = s2.engine.weeks[0].trades
    assert [(t.reason, t.confirm_week, t.nominal_execution_week, t.pipeline_step) for t in first] == [
        (TradeReason.SIGNAL_EXIT, D("2002-12-27"), D("2003-01-03"), 1)]
    assert not [t for t in s2.engine.trades if t.nominal_execution_week == D("2003-01-17")]
    assert not plan["force"]                        # same effective state before step 1
    row = wf.boundary_row(s2, None)
    assert row["cancelled_pending_executions"] == "stocks:2002-12-27->2003-01-17:RISK_OFF"
    assert row["adopted_pending_executions"] == "stocks:2002-12-27->2003-01-03:RISK_OFF"
    assert row["reconstructed_signal_state"] == "stocks" and row["carried_signal_state"] == ""
    assert "ma=4" in row["old_signal_params"] and "ma=6" in row["new_signal_params"]


@pytest.mark.parametrize("sig,expected", [
    (dict(ma_length=4, delay_weeks=3), (D("2002-12-27"), D("2003-01-17"))),   # pending carried
    (dict(ma_length=4, delay_weeks=1, confirm_off_weeks=2), (D("2003-01-03"), D("2003-01-10")))])
def test_same_params_carry_the_live_tracker(tmp_path, sig, expected):
    """Q-022 point 31/58: identical parameters -> the live tracker continues (SMA window,
    confirmation counters, pending executions); it equals the selected training state and the
    execution happens exactly where one continuous run executes it (no confirmation reset)."""
    files = pending_data(tmp_path)
    c = stocks_cfg(files, **sig)
    spec, prepared, windows, sels, path = oos_path_for(c, [c, c, c])
    s2 = path.segments[1]
    assert s2.plan["carried"] == ("stocks",) and s2.plan["replaced"] == ()
    assert s2.plan["matches"] == {"stocks": True} and not s2.plan["cancelled"]
    exits = [(t.confirm_week, t.nominal_execution_week) for t in s2.engine.trades
             if t.reason == TradeReason.SIGNAL_EXIT]
    assert exits[0] == expected
    run_cfg = resolve(["run", "--start", "2002-01-04", "--as-of-date", "2026-09-29", *files,
                       "--weights", "stocks=1"]).with_overrides(
        signal_overrides(("stocks",), **dict(FAST, **sig)))
    one = app.run_portfolio(run_cfg, write=False)
    assert astuples(path.engine.trades) == astuples(one.engine.trades)
    assert astuples(path.engine.signal_records) == astuples(one.engine.signal_records)


# ============================================================================ outputs, determinism
GRID = ["--optimize-params", "weights,ma,delay", "--btc-weight", "0,20", "--gold-weight", "0,20",
        "--ma-grid", "3,6", "--delay-grid", "1,2", *WINDOWS[:-2]]


def test_outputs_manifest_and_journals(tmp_path):
    """REP-016 / WF-005 / WF-016 / Q-022 points 49-54, 67, 69: output files, one
    walk_forward_results row per OOS window with the selected parameters, the full training
    grids, boundary audit events, manifest no-future checks, stitched journals."""
    files = write_market(tmp_path / "m", FIRST, market_returns(FIRST, N, seed=5))
    cfg = wf_config(files, *GRID, "--jobs", "1", "--tax-profile", "individual_pl",
                    "--dividend-tax-mode", "off", "--output-dir", str(tmp_path / "out"),
                    overrides=signal_overrides(**FAST))
    res = wf.run_walk_forward(cfg)
    out = res.output_dir
    assert out.name.endswith("_walk_forward")
    assert {f.name for f in out.iterdir()} == {
        "summary.csv", "weekly_portfolio.csv", "trades.csv", "tax_events.csv", "payments.csv",
        "rf_transfers.csv", "rebalance_events.csv", "signals.csv", "validation_report.csv",
        "config_resolved.yaml", "data_manifest.json", "weekly_normalized.csv", "realizations.csv",
        "dividend_reinvestments.csv", "tax_state.json", "terminal_settlement.json",
        "walk_forward_results.csv", "training_grid_results.csv", "walk_forward_manifest.json",
        "walk_forward_boundary_events.csv"}
    rows = read_csv(out / "walk_forward_results.csv")
    assert list(rows[0]) == wf.RESULT_FIELDS and len(rows) == 3
    for r, sel, w in zip(rows, res.selections, res.windows):
        assert int(r["selected_grid_index"]) == sel.grid_index
        assert int(r["selected_ma"]) == sel.params["stocks"].ma
        assert int(r["selected_delay"]) == sel.params["stocks"].delay
        assert float(r["selected_weight_btc"]) == sel.targets["btc"]
        assert r["selected_threshold"] and r["selected_confirmation"] and r["selected_sell_fraction"]
        assert D(r["max_training_week"]) < D(r["test_start"]) and D(r["train_end"]) < D(r["test_start"])
    assert [float(r["oos_nav_start"]) for r in rows[1:]] == [float(r["oos_nav_end"]) for r in rows[:-1]]
    grid = read_csv(out / "training_grid_results.csv")
    assert list(grid[0]) == wf.TRAINING_FIELDS and len(grid) == 3 * 16
    for w in res.windows:
        g = [x for x in grid if x["window_id"] == str(w.window_id)]
        assert [int(x["grid_index"]) for x in g] == list(range(1, 17))
        assert sum(x["selected"] == "true" for x in g) == 1
        sel = next(x for x in g if x["selected"] == "true")
        assert sel["eligible_rank"] == "1" and float(sel["objective_gap_to_selected"]) == 0.0
        assert all(float(x["objective_gap_to_selected"]) >= 0 for x in g if x["eligible"] == "true")
        assert {x["effective_first_week"] for x in g} == {w.train_start.isoformat()}
        assert {x["effective_last_week"] for x in g} == {w.train_end.isoformat()}
    bnd = read_csv(out / "walk_forward_boundary_events.csv")
    assert list(bnd[0]) == wf.BOUNDARY_FIELDS and [b["week_key"] for b in bnd] == [
        w.test_start.isoformat() for w in res.windows]
    m = json.loads((out / "walk_forward_manifest.json").read_text(encoding="utf-8"))
    assert m["no_future_training_checks_passed"] is True and len(m["windows"]) == 3
    for x in m["windows"]:
        assert x["max_training_week"] < x["test_start"] and x["no_future_training"] is True
        assert len(x["training_input_sha256"]) == 64 and x["selected_grid_index"] >= 1
        assert x["top_eligible"][0]["grid_index"] == x["selected_grid_index"]
        assert x["selected_neighbours"]
    s = read_csv(out / "summary.csv")[0]
    assert (s["optimization_mode"], s["walk_forward_window"], s["train_years"], s["test_years"],
            s["oos_windows"], s["first_oos_week"], s["last_oos_week"]) == (
        "walk-forward", "rolling", "2", "1", "3", "2002-01-04", "2004-06-25")
    assert s["effective_first_week"] == "2002-01-04" and s["inception_date"] == "2001-12-28"
    assert float(s["growth_base_nav"]) == 1_000_000.0
    weekly = read_csv(out / "weekly_portfolio.csv")
    assert [r["week_key"] for r in weekly] == [w.isoformat() for x in res.windows for w in x.oos_weeks]
    trades = read_csv(out / "trades.csv")
    weekly_trades = [t for t in trades if t["phase"] == "weekly"]
    assert len(weekly_trades) == len(res.path.engine.trades) == sum(
        len(sg.engine.trades) for sg in res.path.segments)
    keys = [(t["week_key"], t["asset"], t["side"], t["reason"], t["gross_traded_value"]) for t in trades]
    assert len(keys) == len(set(keys))
    taxes = read_csv(out / "tax_events.csv")
    assert len(taxes) == len(res.portfolio.tax_state.tax_events) + len(
        res.portfolio.terminal.terminal_tax_events)
    dm = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    assert dm["first_return_week"] == "2002-01-04" and dm["walk_forward_manifest"] == wf.MANIFEST


def test_serial_and_parallel_are_identical(tmp_path):
    """Q-022 point 24/63: --jobs parallelizes the candidates of a training window; selections,
    walk_forward_results.csv, training_grid_results.csv, the OOS path, trades, tax events and
    summary.csv are byte-identical to the sequential run."""
    files = write_market(tmp_path / "m", FIRST, market_returns(FIRST, N, seed=8))
    runs = {}
    for j in (1, 2):
        cfg = wf_config(files, *GRID, "--jobs", str(j), "--tax-profile", "individual_pl",
                        "--dividend-tax-mode", "off", "--output-dir", str(tmp_path / f"j{j}"),
                        overrides=signal_overrides(**FAST))
        runs[j] = wf.run_walk_forward(cfg)
    a, b = runs[1].output_dir, runs[2].output_dir
    for f in a.iterdir():
        if f.suffix == ".csv" or f.name in ("tax_state.json", "terminal_settlement.json"):
            assert (b / f.name).read_bytes() == f.read_bytes(), f.name
    ma = json.loads((a / wf.MANIFEST).read_text(encoding="utf-8"))
    mb = json.loads((b / wf.MANIFEST).read_text(encoding="utf-8"))
    assert {k for k in ma if ma[k] != mb[k]} <= {"run_timestamp", "jobs_requested",
                                                 "jobs_effective", "parallel_training",
                                                 "code_version"}
    assert mb["parallel_training"] is True and ma["parallel_training"] is False
    assert [s.grid_index for s in runs[1].selections] == [s.grid_index for s in runs[2].selections]


# ============================================================================ CLI-011
CLI_011 = ["optimize", "--optimization-mode", "walk-forward", "--optimize-params",
           "weights,ma,threshold,delay", "--walk-forward-window", "rolling", "--train-years", "15",
           "--test-years", "5", "--ma-grid", "40,50,60", "--threshold-grid", "0,1,3,5",
           "--delay-grid", "1:4"]


def test_cli_011_exact_on_staged_data_needs_more_history(tmp_path):
    """CLI-011 exactly as specified on the staged data: the BTC-bounded common calendar is far
    shorter than 15 training years + an OOS window -> a clear Q-020 error (exit 1), the
    training window is never shortened and nothing is written."""
    r = subprocess.run([sys.executable, BT, *CLI_011, "--output-dir", str(tmp_path / "o")],
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 1, r.stderr
    assert "insufficient history for requested walk-forward training window" in r.stderr
    assert "train_years=15" in r.stderr
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").iterdir())


def test_cli_011_runs_on_a_long_synthetic_history(tmp_path):
    """CLI-011 functional run through the real CLI on 26.5 synthetic years: every CLI-011 flag
    (weights, MA 40/50/60, threshold 0/1/3/5 %, delay 1..4, rolling, train 15, test 5) with a
    reduced weight grid (btc 0/10 %, gold 0/10 %) -> two full OOS windows and a shorter last
    one, exit 0."""
    files = write_market(tmp_path / "m", "1990-01-05", market_returns("1990-01-05", 1380, seed=11))
    r = subprocess.run([sys.executable, BT, *CLI_011, "--btc-weight", "0,10", "--gold-weight",
                        "0,10", "--jobs", "2", "--as-of-date", "2026-09-29", "--output-dir",
                        str(tmp_path / "o"), *files],
                       capture_output=True, text=True, timeout=900, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next((tmp_path / "o").iterdir())
    rows = read_csv(out / "walk_forward_results.csv")
    assert len(rows) == 3
    assert [float(x["actual_test_years"]) > 4.9 for x in rows] == [True, True, False]
    assert rows[-1]["actual_oos_end"] == "2016-06-10"
    for x in rows:
        assert int(x["selected_ma"]) in (40, 50, 60) and int(x["selected_delay"]) in (1, 2, 3, 4)
        assert float(x["selected_threshold"]) in (0.0, 0.01, 0.03, 0.05)
        assert float(x["selected_weight_btc"]) in (0.0, 0.1)
        assert int(x["train_weeks"]) >= 782
    grid = read_csv(out / "training_grid_results.csv")
    assert len(grid) == 3 * 4 * 3 * 4 * 4
    m = json.loads((out / wf.MANIFEST).read_text(encoding="utf-8"))
    assert m["candidates_per_window"] == 192 and m["no_future_training_checks_passed"] is True
    assert m["parameters"] == ["weights", "ma", "threshold", "delay"]
