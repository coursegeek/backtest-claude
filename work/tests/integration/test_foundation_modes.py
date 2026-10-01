"""Foundation tax modes end to end (FND-002, FND-005, FND-009, Q-037, Q-047): the CLI with a
distribution schedule (outputs, provenance, reconciliation), 15 % vs 19 % in tax-compare, the
non-zero internal trading tax in a full run, and both in the continuous walk-forward OOS path
(one continuous run equivalence, every scheduled row paid exactly once)."""
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from src import app
from src import tax_compare as tc
from src import walk_forward as wf
from src.cli import resolve
from src.models import TradeReason
from src.reporting import SUMMARY_FIELDS, summary_row

from fixtures.market import market_returns, signal_overrides, wf_config, write_market

WORK = Path(__file__).resolve().parents[2]
BT = str(WORK / "backtest.py")
D = dt.date.fromisoformat
RUN = ["run", "--weights", "stocks=0.5,gold=0.3,btc=0.2", "--start", "2018-01-01", "--end",
       "2020-12-31", "--as-of-date", "2026-09-29", "--dividend-tax-mode", "off",
       "--rebalance", "quarterly", "--transaction-cost-bps", "10", "--slippage-bps", "5"]
SCHEDULE = ("date,amount,percent_nav\n"
            "2017-06-02,50000,\n"              # before the run: ignored with a warning
            "2018-06-13,100000,\n"
            "2019-01-02,,0.05\n"               # first week of 2019: admin cost the same week
            "2019-01-03,25000,\n"              # same week again
            "2019-07-01,,0.10\n"               # first week of a quarter: rebalance the same week
            "2023-01-06,10000,\n")             # after the run: ignored with a warning


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_schedule(tmp_path, text=SCHEDULE):
    f = tmp_path / "wyplaty.csv"
    f.write_text(text, encoding="utf-8")
    return f


def test_distribution_schedule_cli_outputs_and_provenance(tmp_path):
    """FND-005 / FND-009 / Q-037 through the CLI (family_foundation_19, distributed_amount):
    distributions.csv, weekly gross = tax + net reconciling the NAV outflow, summary fields,
    TEST-024, no terminal distribution, after_tax_terminal_wealth = remaining NAV + cumulative net
    distributions, schedule provenance (path, SHA-256, rows, dates, kinds) and the warnings for
    rows outside the run."""
    sched = write_schedule(tmp_path)
    r = subprocess.run([sys.executable, BT, *RUN, "--tax-profile", "family_foundation_19",
                        "--foundation-tax-event", "distribution_schedule", "--distribution-file",
                        str(sched), "--output-dir", str(tmp_path / "o")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = next((tmp_path / "o").iterdir())
    dist = read_csv(out / "distributions.csv")
    assert [(x["row_index"], x["scheduled_date"], x["nominal_week"], x["actual_week"], x["kind"])
            for x in dist] == [
        ("2", "2018-06-13", "2018-06-15", "2018-06-15", "amount"),
        ("3", "2019-01-02", "2019-01-04", "2019-01-04", "percent_nav"),
        ("4", "2019-01-03", "2019-01-04", "2019-01-04", "amount"),
        ("5", "2019-07-01", "2019-07-05", "2019-07-05", "percent_nav")]
    for x in dist:
        assert float(x["gross"]) == pytest.approx(float(x["tax"]) + float(x["net"]))
        assert float(x["tax"]) == pytest.approx(0.19 * float(x["tax_base"]))
        assert float(x["tax_base"]) == float(x["gross"])                     # distributed_amount
    weekly = {w["week_key"]: w for w in read_csv(out / "weekly_portfolio.csv")}
    for wk in ("2018-06-15", "2019-01-04", "2019-07-05"):
        w = weekly[wk]
        gross = math.fsum(float(x["gross"]) for x in dist if x["actual_week"] == wk)
        assert float(w["gross_distributions_paid"]) == pytest.approx(gross)
        assert float(w["gross_distributions_paid"]) == pytest.approx(
            float(w["net_distributions_paid"]) + float(w["distribution_tax_paid"]))
        assert float(w["payments"]) == pytest.approx(float(w["costs_paid"]) + float(w["annual_tax_paid"])
                                                     + float(w["net_distributions_paid"]))
        assert float(w["nav_after_signal"]) - float(w["nav_before_returns"]) == pytest.approx(
            float(w["amounts_due"]) + float(w["transaction_costs"]) + float(w["slippage"]))
    assert float(weekly["2019-01-04"]["costs_paid"]) > 0                    # 2018 admin cost too
    assert weekly["2019-07-05"]["rebalance"] == "calendar_rebalance"
    s = read_csv(out / "summary.csv")[0]
    gross = math.fsum(float(x["gross"]) for x in dist)
    net = math.fsum(float(x["net"]) for x in dist)
    assert float(s["foundation_gross_distributions_paid"]) == pytest.approx(gross)
    assert float(s["foundation_net_distributions_paid"]) == pytest.approx(net)
    assert float(s["foundation_distribution_tax_paid"]) == pytest.approx(gross - net)
    assert float(s["distribution_capital_basis_remaining"]) == pytest.approx(1_000_000.0 - gross)
    assert s["distributed_amount"] == "" and float(s["terminal_foundation_tax"]) == 0.0
    events = read_csv(out / "tax_events.csv")
    assert float(s["total_tax_paid"]) == pytest.approx(math.fsum(
        float(e["amount"]) for e in events if e["category"] == "tax"), rel=1e-12)      # TEST-024
    sched_ev = [e for e in events if e["event_type"] == "foundation_distribution_tax"]
    assert {(e["settlement"], e["phase"], e["pipeline_step"]) for e in sched_ev} == {("scheduled", "weekly", "2")}
    term = json.loads((out / "terminal_settlement.json").read_text(encoding="utf-8"))
    assert term["tax_event"] == "distribution_schedule" and term["distributed_amount"] is None
    assert not [t for t in term["liquidation"] if t["reason"] == "foundation_distribution_liquidation"]
    assert float(s["after_tax_terminal_wealth"]) == pytest.approx(term["remaining_nav"] + net)
    trades = read_csv(out / "trades.csv")
    assert not [t for t in trades if t["phase"] == "terminal" and t["side"] == "sell"
                and t["reason"] != "sell_to_pay"]
    m = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    src = next(x for x in m["sources"] if x["role"] == "distribution_schedule")
    assert src["sha256"] == hashlib.sha256(sched.read_bytes()).hexdigest()
    assert (src["path"], src["config_key"], src["raw_rows"], src["raw_first_date"], src["raw_last_date"]) == (
        str(sched), "tax.foundation.distribution_file", 6, "2017-06-02", "2023-01-06")
    assert (src["extra"]["amount_rows"], src["extra"]["percent_nav_rows"]) == (4, 2)
    assert m["foundation_tax_event"] == "distribution_schedule" and m["foundation_limitations"]
    assert m["first_return_week"] == "2018-01-05" and "distributions.csv" in m["audit_outputs"]
    warn = [x for x in read_csv(out / "validation_report.csv") if x["code"] == "distribution_row_ignored"]
    assert sorted(x["message"].split(" (")[0] for x in warn) == [
        "scheduled distribution row 1", "scheduled distribution row 6"]
    # the distribution file is no market source: the common calendar is unchanged
    assert s["common_data_start"] and "distribution" not in s["range_truncation_detail"]


def test_distribution_schedule_missing_or_invalid_file_cli(tmp_path):
    """Q-037: no file key, a missing file or an invalid row stop before any output (exit 2)."""
    base = [sys.executable, BT, *RUN, "--tax-profile", "family_foundation_15",
            "--foundation-tax-event", "distribution_schedule", "--output-dir", str(tmp_path / "o")]
    r = subprocess.run(base, capture_output=True, text=True, timeout=300)
    assert r.returncode == 2 and "requires tax.foundation.distribution_file" in r.stderr
    r = subprocess.run(base + ["--distribution-file", str(tmp_path / "none.csv")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 2 and "not found" in r.stderr
    bad = write_schedule(tmp_path, "date,amount,percent_nav\n2019-01-04,100,0.1\n")
    r = subprocess.run(base + ["--distribution-file", str(bad)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 2 and "row 1" in r.stderr and "exactly one" in r.stderr
    assert not (tmp_path / "o").exists()


def test_distribution_schedule_15_vs_19_in_tax_compare(tmp_path):
    """FND-008 / Q-037 point 34: with one schedule both foundations have the identical weekly NAV
    (gross D leaves both); the distribution tax, net payout, after-tax wealth and after-tax CAGR
    differ; the pre-tax shadow is identical."""
    sched = write_schedule(tmp_path)
    cfg = resolve(["tax-compare", "--weights", "stocks=0.5,gold=0.3,btc=0.2", "--start", "2018-01-01",
                   "--end", "2020-12-31", "--as-of-date", "2026-09-29", "--dividend-tax-mode", "off",
                   "--tax-profile", "family_foundation_15,family_foundation_19",
                   "--foundation-tax-event", "distribution_schedule", "--distribution-file", str(sched),
                   "--output-dir", str(tmp_path / "o")])
    res = tc.run_tax_compare(cfg, write=False)
    a, b = res.results["family_foundation_15"], res.results["family_foundation_19"]
    assert [w.ledger_end for w in a.engine.weeks] == [w.ledger_end for w in b.engine.weeks]
    sa, sb = a.terminal.final_tax_state, b.terminal.final_tax_state
    assert sa.gross_distributions_paid == sb.gross_distributions_paid > 0
    assert sa.distribution_tax_paid == pytest.approx(15 / 19 * sb.distribution_tax_paid)
    assert sa.net_distributions_paid > sb.net_distributions_paid
    assert a.terminal.after_tax_terminal_wealth - b.terminal.after_tax_terminal_wealth == pytest.approx(
        sa.net_distributions_paid - sb.net_distributions_paid)
    assert a.metrics.after_tax_cagr > b.metrics.after_tax_cagr
    assert a.pre_tax.final_wealth == b.pre_tax.final_wealth


def test_internal_trading_tax_full_run_reconciles(tmp_path):
    """Q-047 in a full run (terminal mode): annual internal-tax events per closed year in step 2
    of the first week of the next year, the final year in the terminal settlement before the
    admin cost and the distribution; summary total_tax_paid = sum of the tax events (TEST-024,
    MET-015); internal_trading_tax_paid and terminal_internal_trading_tax reported."""
    cfg = resolve([*RUN, "--tax-profile", "family_foundation_15", "--output-dir", str(tmp_path)]
                  ).with_overrides({"tax.foundation.internal_trading_tax_rate": 0.1})
    res = app.run_portfolio(cfg)
    s = read_csv(res.output_dir / "summary.csv")[0]
    events = read_csv(res.output_dir / "tax_events.csv")
    internal = [e for e in events if e["event_type"] == "foundation_internal_trading_tax"]
    assert [(e["tax_year"], e["settlement"], e["week_key"]) for e in internal] == [
        ("2017", "annual", "2018-01-05"), ("2018", "annual", "2019-01-04"),
        ("2019", "annual", "2020-01-03"), ("2020", "terminal", "2020-12-25")]
    for e in internal:
        assert float(e["amount"]) == pytest.approx(0.1 * max(0.0, float(e["gross_base"])))
    assert float(s["total_tax_paid"]) == pytest.approx(math.fsum(
        float(e["amount"]) for e in events if e["category"] == "tax"), rel=1e-12)
    assert float(s["internal_trading_tax_paid"]) == pytest.approx(math.fsum(float(e["amount"]) for e in internal))
    assert float(s["terminal_internal_trading_tax"]) == pytest.approx(float(internal[-1]["amount"]))
    t = res.terminal
    assert t.distributed_amount == pytest.approx(t.nav_after_liquidation - t.terminal_internal_trading_tax
                                                 - t.final_admin_cost.amount)
    assert res.pre_tax.engine.final_ledger != res.engine.final_ledger              # shadow: no tax
    assert not res.pre_tax.terminal_cost.final_state.internal_tax_by_year


# ------------------------------------------------------------------ walk-forward
FIRST, N = "1999-01-01", 287
FAST = dict(ma_length=3, confirm_off_weeks=1, confirm_on_weeks=1, sell_fraction=0.5,
            threshold_off=0.0, threshold_on=0.0)
WINDOWS = ["--train-years", "2", "--test-years", "1", "--start", "1999-12-31", "--jobs", "1"]
WF_ONLY = {"run_name", "optimization_mode", "walk_forward_window", "train_years", "test_years",
           "step_years", "oos_windows", "first_oos_week", "last_oos_week", "requested_start",
           "requested_end"}


@pytest.mark.parametrize("mode", ["internal_tax", "schedule"])
def test_walk_forward_foundation_state_equals_one_continuous_run(tmp_path, mode):
    """Q-022 x Q-047 / Q-037: with a constant selection the stitched OOS path equals one
    continuous run - the annual internal trading tax of each closed year (determined in the
    first week of the next window) or the scheduled distributions with the cumulative gain_only
    basis carried across window boundaries; each scheduled row is paid exactly once (a row in
    the training history only is ignored by the OOS path with a warning)."""
    files = write_market(tmp_path / "m", FIRST, market_returns(FIRST, N, seed=14))
    argv = ["--weights", "stocks=0.5,gold=0.3,btc=0.2", "--tax-profile", "family_foundation_19",
            "--dividend-tax-mode", "off", "--transaction-cost-bps", "10", "--rebalance", "monthly"]
    over = dict(signal_overrides(**FAST))
    if mode == "internal_tax":
        over["tax.foundation.internal_trading_tax_rate"] = 0.1
    else:
        sched = write_schedule(tmp_path, "date,amount,percent_nav\n2001-03-07,90000,\n"
                                         "2002-05-15,200000,\n2003-01-01,,0.10\n2003-08-13,150000,\n"
                                         "2004-03-03,,0.05\n")
        argv += ["--foundation-tax-event", "distribution_schedule", "--distribution-file", str(sched)]
    cfg = wf_config(files, "--optimize-params", "delay", "--delay-grid", "1", *WINDOWS, *argv,
                    overrides=over)
    res = wf.run_walk_forward(cfg, write=False)
    ts = [w.test_start for w in res.windows]
    assert ts == [D("2002-01-04"), D("2003-01-03"), D("2004-01-02")]
    run_cfg = resolve(["run", "--start", "2002-01-04", "--as-of-date", "2026-09-29", *files,
                       *argv]).with_overrides(over)
    one = app.run_portfolio(run_cfg, write=False)
    a, b = res.portfolio, one
    assert [(w.week_key, w.ledger_end) for w in a.engine.weeks] == [(w.week_key, w.ledger_end)
                                                                     for w in b.engine.weeks]
    assert [dataclasses.astuple(e) for e in a.terminal.final_tax_state.tax_events] == \
        [dataclasses.astuple(e) for e in b.terminal.final_tax_state.tax_events]
    assert a.terminal.after_tax_terminal_wealth == b.terminal.after_tax_terminal_wealth
    assert a.pre_tax.final_wealth == b.pre_tax.final_wealth
    ra, rb = summary_row(res.spec.base, a), summary_row(run_cfg, b)
    assert {k for k in SUMMARY_FIELDS if ra[k] != rb[k]} <= WF_ONLY
    fa = a.terminal.final_tax_state
    if mode == "internal_tax":
        years = [(e.tax_year, e.week_key) for e in fa.tax_events
                 if e.event_type == "foundation_internal_trading_tax"]
        assert years == [(2001, D("2002-01-04")), (2002, D("2003-01-03")), (2003, D("2004-01-02")),
                         (2004, D("2004-06-25"))]
        assert fa.internal_trading_tax_paid > 0
        return
    paid = [(e.row_index, e.actual_week) for e in fa.distribution_events]
    assert paid == [(2, D("2002-05-17")), (3, D("2003-01-03")), (4, D("2003-08-15")),
                    (5, D("2004-03-05"))]                                     # each row once
    seg_rows = [[e.row_index for e in s.state.tax_state.distribution_events] for s in res.path.segments]
    assert seg_rows == [[2], [2, 3, 4], [2, 3, 4, 5]]                        # carried, not repeated
    basis = [s.state.tax_state.distribution_capital_basis_remaining for s in res.path.segments]
    assert basis[0] == 1_000_000.0 - 200_000.0 and basis[-1] == fa.distribution_capital_basis_remaining
    assert basis[-1] == pytest.approx(1_000_000.0 - fa.gross_distributions_paid)
    assert fa.distribution_tax_paid == pytest.approx(0.19 * fa.gross_distributions_paid)
    assert sum(1 for e in res.path.shadow_segments[-1].state.tax_state.distribution_events) == 4
    warn = [i for i in res.portfolio.report.issues if i.code == "distribution_row_ignored"]
    assert [i.message.split(" (")[0] for i in warn] == ["scheduled distribution row 1"]
    assert {t.reason for t in a.engine.trades} <= {TradeReason.CALENDAR_REBALANCE, TradeReason.SIGNAL_EXIT,
                                                  TradeReason.SIGNAL_REENTRY, TradeReason.SELL_TO_PAY,
                                                  TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION}
