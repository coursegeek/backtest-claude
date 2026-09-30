"""`run` with tax.profile=none (signal-only and strategic rebalancing) on staged data
(mechanics only; staged proxy files never serve as canonical-data evidence)."""
import csv
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from src.app import run_portfolio
from src.config import ResolvedConfig
from src.errors import NotImplementedCommand, WarmupError

WORK = Path(__file__).resolve().parents[2]
S07 = {"allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
       "run": {"start": "2018-01-01", "end": "2026-07-31", "as_of_date": "2026-09-29"}}


def cfg(extra=None, out=None):
    layer = json.loads(json.dumps(S07))
    for k, v in (extra or {}).items():
        layer.setdefault(k, {}).update(v)
    if out:
        layer["report"] = {"output_dir": str(out), "run_name": "t"}
    return ResolvedConfig("run", cli_layer=layer)


def test_run_signal_only_staged_mechanics(tmp_path):
    res = run_portfolio(cfg(out=tmp_path))
    eng = res.engine
    assert res.first_week == dt.date(2018, 1, 5) and res.last_week == dt.date(2026, 7, 31)
    assert len(eng.weeks) == 448 and eng.trades
    assert {t.reason.value for t in eng.trades} <= {"signal_exit", "signal_reentry"}
    assert eng.initial_ledger.nav == 1_000_000.0
    for a, b in zip(eng.weeks, eng.weeks[1:]):
        assert a.nav_end == b.nav_start                       # continuous NAV path
    confirmations = [r for r in eng.signal_records if r.confirmation
                     and r.scheduled_execution_week <= res.last_week]
    assert len(eng.trades) <= len(confirmations)
    out = res.output_dir
    for name in ("weekly_portfolio.csv", "trades.csv", "signals.csv", "validation_report.csv",
                 "weekly_normalized.csv", "config_resolved.yaml", "data_manifest.json"):
        assert (out / name).is_file(), name
    rows = list(csv.DictReader((out / "weekly_portfolio.csv").open()))
    last = rows[-1]
    comps = ["value_stocks", "value_gold", "value_btc", "value_rf_base", "value_rf_reserve_stocks",
             "value_rf_reserve_gold", "value_rf_reserve_btc"]
    assert abs(math.fsum(float(last[c]) for c in comps) - float(last["nav_end"])) < 1e-6
    assert float(last["target_stocks"]) == 0.6 and float(last["weight_end_stocks"]) != 0.6
    m = json.loads((out / "data_manifest.json").read_text())
    assert m["run_calendar_weeks"] == 448 and m["pre_tax_method"] == "actual_run_no_taxes"
    assert (out / "summary.csv").is_file()
    assert "range_truncated" in (out / "validation_report.csv").read_text()


def test_run_is_deterministic(tmp_path):
    """REPRO-006 for run outputs (manifest timestamp and directory name aside)."""
    a = run_portfolio(cfg(out=tmp_path / "a")).output_dir
    b = run_portfolio(cfg(out=tmp_path / "b")).output_dir
    for name in ("weekly_portfolio.csv", "trades.csv", "signals.csv", "validation_report.csv",
                 "weekly_normalized.csv"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_s07_band_rebalance_run(tmp_path):
    """S07 mechanics: band 1 pp on staged data; every end-of-week breach is followed by a
    band rebalance at the next retained week that restores the targets; weight_start_* in
    weekly_portfolio.csv comes from the post-rebalance ledger (PORT-012, REP-013)."""
    res = run_portfolio(cfg({"portfolio": {"rebalance": "band", "rebalance_band_pp": 1}},
                            out=tmp_path / "a"))
    eng, out = res.engine, res.output_dir
    assert eng.rebalance_events and {t.reason.value for t in eng.trades} <= {
        "signal_exit", "signal_reentry", "band_rebalance"}
    for name in ("payments.csv", "rf_transfers.csv", "rebalance_events.csv"):
        assert (out / name).is_file(), name
    events = {e.week_key: e for e in eng.rebalance_events}
    t = eng.weeks[0].ledger_before_returns.sleeve_weights().keys()
    for prev, w in zip(eng.weeks, eng.weeks[1:]):
        dev = max(abs(v - {"stocks": 0.6, "gold": 0.2, "btc": 0.2, "rf": 0.0}[s])
                  for s, v in prev.ledger_end.sleeve_weights().items())
        assert (w.week_key in events) == (dev >= 0.01 - 1e-12), w.week_key
        if w.week_key in events:
            assert events[w.week_key].trigger_source_week == prev.week_key
            for s, v in w.ledger_before_returns.sleeve_weights().items():
                assert abs(v - {"stocks": 0.6, "gold": 0.2, "btc": 0.2, "rf": 0.0}[s]) < 1e-12
    rows = list(csv.DictReader((out / "weekly_portfolio.csv").open()))
    for r, w in zip(rows, eng.weeks):
        for s in t:
            assert float(r[f"weight_start_{s}"]) == w.ledger_before_returns.sleeve_weights()[s]
    b = run_portfolio(cfg({"portfolio": {"rebalance": "band", "rebalance_band_pp": 1}},
                          out=tmp_path / "b")).output_dir
    for name in ("weekly_portfolio.csv", "trades.csv", "rebalance_events.csv", "rf_transfers.csv",
                 "payments.csv"):
        assert (out / name).read_bytes() == (b / name).read_bytes(), name


@pytest.mark.parametrize("mode,count", [("monthly", 102), ("quarterly", 34), ("yearly", 8),
                                        ("weekly", 447)])
def test_calendar_modes_run(mode, count):
    """REB-006: Jan 2018 .. Jul 2026 has 103 months, 35 quarters, 9 years; the first record is
    the initial allocation, so one fewer trigger each."""
    eng = run_portfolio(cfg({"portfolio": {"rebalance": mode, "transaction_cost_bps": 10.0}}),
                        write=False).engine
    assert len(eng.rebalance_events) == count
    assert all(t.transaction_cost > 0 for t in eng.trades)


def test_run_costs_reduce_nav_monotonically():
    free = run_portfolio(cfg(), write=False).engine
    costly = run_portfolio(cfg({"portfolio": {"transaction_cost_bps": 25.0, "slippage_bps": 10.0}}),
                           write=False).engine
    assert [t.week_key for t in free.trades] == [t.week_key for t in costly.trades]
    assert costly.weeks[-1].nav_end < free.weeks[-1].nav_end


@pytest.mark.parametrize("extra", [
    {"tax": {"profile": "family_foundation_15", "foundation": {"tax_event": "distribution_schedule"}}},
    {"tax": {"profile": "family_foundation_19", "foundation": {"internal_trading_tax_rate": 0.1}}},
    {"tax": {"profile": "family_foundation_19", "foundation": {"tax_event": "distribution_schedule"}},
     "portfolio": {"rebalance": "band", "rebalance_band_pp": 1}}])
def test_unsupported_modes_are_refused(extra):
    with pytest.raises(NotImplementedCommand):
        run_portfolio(cfg(extra), write=False)


def test_s01_gold_warmup_error():
    """Q-013 proposal: CLI-001 start 1971-01-01 lacks gold warm-up on staged data."""
    c = ResolvedConfig("run", cli_layer={"allocation": {"targets": "stocks=0.4,gold=0.4,rf=0.2"},
                                         "run": {"start": "1971-01-01", "end": "2026-07-31",
                                                 "as_of_date": "2026-09-29"}})
    with pytest.raises(WarmupError):
        run_portfolio(c, write=False)


def test_run_cli_exit_codes(tmp_path):
    base = [sys.executable, str(WORK / "backtest.py"), "run", "--weights", "stocks=0.6,gold=0.2,btc=0.2",
            "--start", "2018-01-01", "--end", "2026-07-31", "--as-of-date", "2026-09-29"]
    ok = subprocess.run(base + ["--output-dir", str(tmp_path)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    taxed = subprocess.run(base + ["--tax-profile", "family_foundation_15", "--foundation-tax-event",
                                   "distribution_schedule"], capture_output=True, text=True)
    assert taxed.returncode == 3 and "distribution_schedule is not implemented; Q-037 remains open" \
        in taxed.stderr
    opt = subprocess.run([sys.executable, str(WORK / "backtest.py"), "optimize", "--start",
                          "2018-01-01", "--optimization-mode", "walk-forward"],
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert opt.returncode == 3 and "not implemented" in opt.stderr


def test_end_defaults_to_last_common_week():
    """RUN-002 / NORM-011: without --end the run stops at the last common return week
    (Fama-French ends 2026-08-28 while prices continue to 2026-09-18)."""
    c = ResolvedConfig("run", cli_layer={"allocation": {"targets": "stocks=0.6,gold=0.4"},
                                         "run": {"start": "2024-01-01", "as_of_date": "2026-09-29"}})
    res = run_portfolio(c, write=False)
    assert res.last_week == dt.date(2026, 8, 28) and res.engine.weeks[-1].week_key == res.last_week
    assert any(i.code == "range_truncated" and i.role == "gold" and "end" in i.message
               for i in res.report.issues)


def test_btc_file_required_only_when_used(tmp_path):
    """DATA-005: the BTC file is needed only when BTC has a weight (or its signal is tested)."""
    from src.errors import DataFileNotFound
    missing = {"data": {"btc_file": str(tmp_path / "no_btc.csv")}}
    no_btc = ResolvedConfig("run", cli_layer={**missing, "allocation": {"targets": "stocks=1"},
                                              "run": {"start": "2020-01-01", "end": "2020-12-31",
                                                      "as_of_date": "2026-09-29"}})
    assert run_portfolio(no_btc, write=False).engine.weeks
    with_btc = ResolvedConfig("run", cli_layer={**missing, "allocation": {"targets": "stocks=0.9,btc=0.1"},
                                                "run": {"start": "2020-01-01", "end": "2020-12-31",
                                                        "as_of_date": "2026-09-29"}})
    with pytest.raises(DataFileNotFound, match="data.btc_file"):
        run_portfolio(with_btc, write=False)


def test_cli_008_run_with_data_file_override(tmp_path):
    """CLI-008: run --data-file stocks_price=<copy> --weights stocks=1.0 (no --start: the
    earliest common week; the 1933 market closure is a reported common gap)."""
    src = WORK.parent / "input" / "data" / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"
    copy = tmp_path / "moj_spx.csv"
    copy.write_bytes(src.read_bytes())
    r = subprocess.run([sys.executable, str(WORK / "backtest.py"), "run", "--data-file",
                        f"stocks_price={copy}", "--weights", "stocks=1.0", "--as-of-date",
                        "2026-09-29", "--output-dir", str(tmp_path / "o")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = next((tmp_path / "o").iterdir())
    m = json.loads((out / "data_manifest.json").read_text())
    assert m["first_return_week"] == "1926-07-02" and m["common_calendar_gaps"] == ["1933-03-10"]
    assert str(copy) in json.dumps(m["sources"])
