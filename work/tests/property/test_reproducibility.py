"""REPRO-006 for the implemented commands (signals, run, tax-compare - the latter in
tests/integration/test_tax_compare.py): identical inputs/config give identical output files;
only the manifest timestamp and the run directory name differ."""
import json

import pytest

from src.app import run_portfolio, run_signals
from src.config import ResolvedConfig


def test_signal_outputs_identical_except_timestamp(tmp_path):
    dirs = []
    for i in range(2):
        cfg = ResolvedConfig("signals", cli_layer={
            "run": {"asset": "btc", "as_of_date": "2026-09-29"},
            "report": {"output_dir": str(tmp_path / f"o{i}"), "run_name": "repro"}})
        dirs.append(run_signals(cfg).output_dir)
    names = sorted(p.name for p in dirs[0].iterdir())
    assert names == sorted(p.name for p in dirs[1].iterdir())
    assert names == ["config_resolved.yaml", "data_manifest.json", "signals.csv",
                     "validation_report.csv", "weekly_normalized.csv"]
    for n in names:
        a, b = (dirs[0] / n).read_bytes(), (dirs[1] / n).read_bytes()
        if n == "data_manifest.json":
            ja, jb = json.loads(a), json.loads(b)
            ja.pop("run_timestamp"), jb.pop("run_timestamp")
            ja["code_version"].pop("working_tree_dirty"), jb["code_version"].pop("working_tree_dirty")
            assert ja == jb
        elif n == "config_resolved.yaml":
            assert a.replace(str(tmp_path / "o0").encode(), b"X") == b.replace(str(tmp_path / "o1").encode(), b"X")
        else:
            assert a == b, n


def _same_except_timestamp(dirs, tmp_path):
    names = sorted(p.name for p in dirs[0].iterdir())
    assert names == sorted(p.name for p in dirs[1].iterdir())
    for n in names:
        a, b = (dirs[0] / n).read_bytes(), (dirs[1] / n).read_bytes()
        if n == "data_manifest.json":
            ja, jb = json.loads(a), json.loads(b)
            for j in (ja, jb):
                j.pop("run_timestamp")
                j["code_version"].pop("working_tree_dirty")
            assert ja == jb
        elif n == "config_resolved.yaml":
            assert a.replace(str(tmp_path / "o0").encode(), b"X") == \
                b.replace(str(tmp_path / "o1").encode(), b"X")
        else:
            assert a == b, n
    return names


@pytest.mark.parametrize("profile", ["none", "individual_pl", "family_foundation_19"])
def test_portfolio_run_outputs_identical_except_timestamp(tmp_path, profile):
    """Every file of a portfolio run (weekly path, trades, taxes, terminal settlement, state,
    summary, validation, normalized data) is byte-identical for identical config and inputs."""
    dirs = []
    for i in range(2):
        cfg = ResolvedConfig("run", cli_layer={
            "allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
            "run": {"start": "2020-01-01", "end": "2026-07-31", "as_of_date": "2026-09-29"},
            "portfolio": {"rebalance": "quarterly", "transaction_cost_bps": 10.0,
                          "slippage_bps": 5.0},
            "tax": {"profile": profile},
            "report": {"output_dir": str(tmp_path / f"o{i}"), "run_name": "repro"}})
        dirs.append(run_portfolio(cfg).output_dir)
    names = _same_except_timestamp(dirs, tmp_path)
    assert {"summary.csv", "weekly_portfolio.csv", "trades.csv", "tax_events.csv",
            "payments.csv"} <= set(names)
    assert ("terminal_settlement.json" in names) == (profile != "none")
