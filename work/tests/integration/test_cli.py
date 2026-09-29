"""CLI and import entry points (META-001, CLI-001..011 in their current build scope)."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORK = Path(__file__).resolve().parents[2]
BT = str(WORK / "backtest.py")


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, BT, *args], capture_output=True, text=True, cwd=cwd,
                          timeout=120)


def test_cli_and_import_entrypoints(tmp_path):
    """META-001: backtest.py works as CLI and as an importable module."""
    r = cli("signals", "--asset", "btc", "--as-of-date", "2026-09-29", "--output-dir",
            str(tmp_path), "--run-name", "t")
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    assert out.name.endswith("_t") and (out / "signals.csv").is_file()
    header = (out / "signals.csv").read_text().splitlines()[0].split(",")
    for col in ("price", "sma", "lower_band", "upper_band", "raw_condition", "exit_counter",
                "entry_counter", "confirmed_signal", "scheduled_execution_week",
                "confirmed_state", "effective_state"):                      # REP-006, REP-014
        assert col in header
    sys.path.insert(0, str(WORK))
    import backtest
    cfg = backtest.resolve_config(["signals", "--asset", "gold"])
    assert cfg.get("run.asset") == "gold" and callable(backtest.main)


def test_print_config_precedence(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("run:\n  start: '2000-01-07'\nportfolio:\n  slippage_bps: 7\n", encoding="utf-8")
    r = cli("run", "--config", str(f), "--start", "2018-01-01", "--print-config",
            "--as-of-date", "2026-09-29")
    data = yaml.safe_load(r.stdout)
    assert r.returncode == 0
    assert data["run"]["start"] == "2018-01-01" and data["portfolio"]["slippage_bps"] == 7
    assert data["run"]["as_of_date"] == "2026-09-29"


def test_cli_007_bare_command_fails_with_alloc_001():
    """Q-024: CLI-007 without weights must fail clearly (ALLOC-001)."""
    r = cli("run", "--stocks-price-file", "dane/inny_spx.csv", "--start", "1971-01-01",
            "--end", "2026-07-31")
    assert r.returncode == 2 and "ALLOC-001" in r.stderr


def test_portfolio_commands_validate_then_stop():
    """Commands outside this build resolve/validate their config and stop with exit 3."""
    r = cli("tax-compare", "--weights", "stocks=0.4,gold=0.4,rf=0.2",
            "--tax-profile", "none,individual_pl")
    assert r.returncode == 3 and "not implemented" in r.stderr
    r = cli("delay-scan", "--delay", "1:4")
    assert r.returncode == 2 and "ALLOC-002" in r.stderr
    r = cli("run", "--weights", "stocks=0.7,gold=0.4")
    assert r.returncode == 2 and "sum to 1" in r.stderr
    r = cli("run", "--weights", "stocks=1", "--currency", "USD")
    assert r.returncode == 2 and "RUN-004" in r.stderr


def test_cli_008_data_file_key(tmp_path):
    """CLI-008 / DATA-008: --data-file stocks_price=<path> overrides the source."""
    src = Path(WORK).parent / "input" / "data" / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"
    copy = tmp_path / "moj_spx.csv"
    copy.write_bytes(src.read_bytes())
    r = cli("signals", "--data-file", f"stocks_price={copy}", "--asset", "stocks",
            "--as-of-date", "2026-09-29", "--output-dir", str(tmp_path / "o"))
    assert r.returncode == 0, r.stderr
    manifest = next((tmp_path / "o").iterdir()) / "data_manifest.json"
    assert str(copy) in manifest.read_text()
    r = cli("signals", "--data-file", "spx=x.csv")
    assert r.returncode == 2 and "DATA-008" in r.stderr


def test_missing_data_file_error(tmp_path):
    """ERR-001 through the CLI."""
    r = cli("signals", "--asset", "gold", "--gold-file", str(tmp_path / "none.csv"))
    assert r.returncode == 1 and "data.gold_file" in r.stderr and "none.csv" in r.stderr


def test_cli_009_band_rebalance(tmp_path):
    """CLI-009 (mechanics on staged data): the band 1 pp command runs end-to-end from the CLI
    and writes the rebalance audit files. summary.csv (metrics) is not implemented yet."""
    r = cli("run", "--weights", "stocks=0.6,gold=0.2,btc=0.2", "--rebalance", "band",
            "--rebalance-band-pp", "1", "--start", "2018-01-01", "--end", "2026-07-31",
            "--as-of-date", "2026-09-29", "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    resolved = yaml.safe_load((out / "config_resolved.yaml").read_text())
    assert resolved["portfolio"]["rebalance"] == "band"
    assert resolved["portfolio"]["rebalance_band_pp"] == 1
    events = (out / "rebalance_events.csv").read_text().splitlines()
    assert len(events) > 1 and all(",band,band_rebalance," in e for e in events[1:])
    assert "band_rebalance" in (out / "trades.csv").read_text()
