"""TEST-020 / REPRO-006: the same resolved configuration, the same explicit as_of_date, the same
input files (SHA-256) and source policies and the same code give identical results. Two
complete CLI runs per tax profile are compared file by file; data_manifest.json may differ only
in run_timestamp. A repeated walk-forward (jobs=1) is compared the same way."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import walk_forward as wf

from fixtures.market import market_returns, signal_overrides, wf_config, write_market

WORK = Path(__file__).resolve().parents[2]
BT = str(WORK / "backtest.py")
TIMESTAMP_ONLY = {"run_timestamp"}                     # the only field allowed to differ
COMMON = ["run", "--weights", "stocks=0.5,gold=0.2,btc=0.2,rf=0.1", "--rebalance", "monthly",
          "--transaction-cost-bps", "10", "--slippage-bps", "5", "--start", "2018-01-01",
          "--end", "2026-07-31", "--as-of-date", "2026-09-29", "--run-name", "test020"]
DIVIDENDS = ["--dividend-tax-mode", "smoothed_weekly", "--dividend-file",
             "SPX_dividend_return_weekly_1970_2026.csv"]
DETERMINISTIC = ["summary.csv", "weekly_portfolio.csv", "trades.csv", "tax_events.csv",
                 "payments.csv", "rf_transfers.csv", "rebalance_events.csv", "signals.csv",
                 "realizations.csv", "dividend_reinvestments.csv", "config_resolved.yaml",
                 "weekly_normalized.csv", "validation_report.csv"]
PROFILE_FILES = {"none": ["tax_state.json"],
                 "individual_pl": ["tax_state.json", "terminal_settlement.json"],
                 "family_foundation_19": ["foundation_state.json", "terminal_settlement.json"]}


def without(doc: dict, keys) -> dict:
    return {k: v for k, v in doc.items() if k not in keys}


def compare_dirs(a: Path, b: Path, manifests=("data_manifest.json",)):
    files_a, files_b = sorted(f.name for f in a.iterdir()), sorted(f.name for f in b.iterdir())
    assert files_a == files_b
    for name in files_a:
        if name in manifests:
            ma = json.loads((a / name).read_text(encoding="utf-8"))
            mb = json.loads((b / name).read_text(encoding="utf-8"))
            assert {k for k in ma if ma[k] != mb.get(k)} <= TIMESTAMP_ONLY, name
            assert without(ma, TIMESTAMP_ONLY) == without(mb, TIMESTAMP_ONLY), name
        else:
            assert (a / name).read_bytes() == (b / name).read_bytes(), name
    return files_a


@pytest.mark.parametrize("profile", ["none", "individual_pl", "family_foundation_19"])
def test_identical_outputs_except_timestamp(tmp_path, profile):
    """TEST-020: two identical runs (same command, config, as_of_date, files, policies, code)
    -> every deterministic result file is byte-identical; data_manifest.json differs at most in
    run_timestamp (SHA-256, source policies, config and code version are compared as they are)."""
    argv = COMMON + ["--tax-profile", profile, "--output-dir", str(tmp_path / "out")]
    if profile != "none":
        argv += DIVIDENDS
    for _ in range(2):
        r = subprocess.run([sys.executable, BT, *argv], capture_output=True, text=True,
                           timeout=300, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
    a, b = sorted((tmp_path / "out").iterdir())
    files = compare_dirs(a, b)
    assert set(DETERMINISTIC + PROFILE_FILES[profile]) <= set(files)
    m = json.loads((a / "data_manifest.json").read_text(encoding="utf-8"))
    roles = {s["role"] for s in m["sources"]}
    assert {"stocks_return", "stocks_price", "gold", "btc", "cpi"} <= roles
    assert ("dividend" in roles) == (profile != "none")
    for s in m["sources"]:                       # SHA-256 and source policy of every input
        assert len(s["sha256"]) == 64 and s["adapter"] and s["config_key"]
    assert m["code_version"]["git_commit"] and m["as_of_date"] == "2026-09-29"


def test_walk_forward_repeated_runs_identical(tmp_path):
    """No hidden nondeterminism in walk-forward: the same small walk-forward run twice with
    jobs=1 gives byte-identical CSV/JSON outputs (manifests: run_timestamp only)."""
    first = "1999-01-01"
    files = write_market(tmp_path / "m", first, market_returns(first, 287, seed=20))
    sig = signal_overrides(ma_length=3, confirm_off_weeks=1, confirm_on_weeks=1,
                           threshold_off=0.0, threshold_on=0.0)
    outs = []
    for _ in range(2):
        cfg = wf_config(files, "--optimize-params", "weights,ma,delay", "--btc-weight", "0,20",
                        "--gold-weight", "0,20", "--ma-grid", "3,6", "--delay-grid", "1,2",
                        "--train-years", "2", "--test-years", "1", "--start", "1999-12-31",
                        "--jobs", "1", "--tax-profile", "individual_pl", "--dividend-tax-mode",
                        "off", "--output-dir", str(tmp_path / "out"), overrides=sig)
        outs.append(wf.run_walk_forward(cfg).output_dir)
    files = compare_dirs(*outs, manifests=("data_manifest.json", wf.MANIFEST))
    assert {"walk_forward_results.csv", "training_grid_results.csv",
            "walk_forward_boundary_events.csv", "summary.csv", "trades.csv"} <= set(files)
