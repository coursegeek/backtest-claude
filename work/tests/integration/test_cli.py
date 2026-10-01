"""CLI and import entry points (META-001, CLI-001..011)."""
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fixtures.market import market_returns, write_market

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


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def only_dir(path):
    dirs = [d for d in Path(path).iterdir() if d.is_dir()]
    assert len(dirs) == 1, dirs
    return dirs[0]


STANDARD = ("summary.csv", "weekly_portfolio.csv", "trades.csv", "tax_events.csv",
            "payments.csv", "rf_transfers.csv", "rebalance_events.csv", "signals.csv",
            "validation_report.csv", "config_resolved.yaml", "data_manifest.json",
            "weekly_normalized.csv")
CLI_001 = ["run", "--weights", "stocks=0.4,gold=0.4,rf=0.2", "--start", "1971-01-01",
           "--end", "2026-07-31"]


def test_cli_001_run_weights(tmp_path):
    """CLI-001 / Q-013: the exact command is a full portfolio backtest with explicit weights.
    (1) On sources with enough gold history before 1971 (synthetic, 1960..2026) it runs end to
    end from 1971-01-01. (2) On the staged package (gold proxy starting 1970, Q-002 DATA
    BLOCKER) it stops with ERR-003 - gold: available 51 < required 55 - without moving the
    explicit start or shortening the warm-up. (3) The explicit signal.initial_state=RISK_ON
    opt-in is the only fallback: the run starts, validation_report.csv warns, summary.csv and
    data_manifest.json name the fallback asset."""
    files = write_market(tmp_path / "m", "1960-01-01",
                         market_returns("1960-01-01", 3475, seed=1, assets=("stocks", "gold")))
    r = cli(*CLI_001, *files, "--output-dir", str(tmp_path / "ok"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = only_dir(tmp_path / "ok")
    for name in STANDARD:
        assert (out / name).is_file(), name
    s = read_csv(out / "summary.csv")[0]
    assert (s["effective_first_week"], s["effective_last_week"]) == ("1971-01-01", "2026-07-31")
    assert (s["target_stocks"], s["target_gold"], s["target_rf"]) == ("0.4", "0.4", "0.2")
    assert s["signal_initial_state"] == "reconstruct_history" and s["warmup_fallback_assets"] == ""
    assert s["cagr"] and s["max_drawdown"] and int(s["weeks"]) == 2901          # every Friday
    weekly = read_csv(out / "weekly_portfolio.csv")
    assert (weekly[0]["week_key"], weekly[-1]["week_key"]) == ("1971-01-01", "2026-07-31")
    # (2) staged package: ERR-003, nothing written
    r = cli(*CLI_001, "--output-dir", str(tmp_path / "staged"), cwd=tmp_path)
    assert r.returncode == 1
    assert "ERR-003" in r.stderr and "gold: available 51 < required 55" in r.stderr
    assert "1971-01-01" in r.stderr and "Q-002" in r.stderr
    assert not (tmp_path / "staged").exists()
    # (3) explicit RISK_ON opt-in
    conf = tmp_path / "risk_on.yaml"
    conf.write_text("signal:\n  initial_state: RISK_ON\n", encoding="utf-8")
    r = cli(*CLI_001, "--config", str(conf), "--output-dir", str(tmp_path / "fb"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = only_dir(tmp_path / "fb")
    s = read_csv(out / "summary.csv")[0]
    assert s["effective_first_week"] == "1971-01-01"
    assert (s["signal_initial_state"], s["warmup_fallback_assets"]) == ("RISK_ON", "gold")
    warn = [x for x in read_csv(out / "validation_report.csv") if x["code"] == "warmup_short"]
    assert [(x["severity"], x["role"]) for x in warn] == [("WARNING", "gold")]
    assert "available 51 < required 55" in warn[0]["message"]
    m = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    assert m["signal_initial_state"] == "RISK_ON"
    assert [x["asset"] for x in m["initial_state_fallback"]] == ["gold"]


def test_cli_007_stocks_price_override(tmp_path):
    """CLI-007 / Q-024: (A) the literal example has no weights: --stocks-price-file is parsed
    and resolved, then the run stops with ALLOC-001 (no hidden allocation). (B) The same
    override with --weights stocks=1.0 runs end to end on the fixture dane/inny_spx.csv;
    data_manifest.json records its path and SHA-256."""
    literal = ["run", "--stocks-price-file", "dane/inny_spx.csv", "--start", "1971-01-01",
               "--end", "2026-07-31"]
    r = cli(*literal, "--print-config", cwd=tmp_path)
    assert r.returncode == 0 and yaml.safe_load(r.stdout)["data"]["stocks_price_file"] == \
        "dane/inny_spx.csv"
    r = cli(*literal, cwd=tmp_path)
    assert r.returncode == 2 and "ALLOC-001" in r.stderr
    assert not (tmp_path / "results").exists()
    staged = WORK.parent / "input" / "data" / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"
    rows = read_csv(staged)
    (tmp_path / "dane").mkdir()
    fixture = tmp_path / "dane" / "inny_spx.csv"
    with fixture.open("w", encoding="utf-8", newline="") as f:      # a different SPX file
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["week_start", "price_index_continuous"])
        for row in rows:
            w.writerow([row["week_start"], repr(float(row["price_index_continuous"]) * 10.0)])
    r = cli(*literal, "--weights", "stocks=1.0", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = only_dir(tmp_path / "results")
    for name in STANDARD:
        assert (out / name).is_file(), name
    m = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    src = next(x for x in m["sources"] if x["role"] == "stocks_price")
    assert src["config_key"] == "data.stocks_price_file" and src["path"].endswith("dane/inny_spx.csv")
    assert src["sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest() != \
        hashlib.sha256(staged.read_bytes()).hexdigest()
    s = read_csv(out / "summary.csv")[0]
    assert (s["effective_first_week"], s["effective_last_week"]) == ("1971-01-01", "2026-07-31")
    assert yaml.safe_load((out / "config_resolved.yaml").read_text())["data"]["stocks_price_file"] \
        == "dane/inny_spx.csv"


def test_portfolio_commands_validate_then_stop():
    """Features outside this build resolve/validate their config and stop with exit 3;
    invalid configurations stop with exit 2 before any data is loaded."""
    r = cli("run", "--weights", "stocks=1", "--tax-profile", "family_foundation_15",
            "--foundation-tax-event", "distribution_schedule")
    assert r.returncode == 3 and "Q-037" in r.stderr
    r = cli("optimize", "--optimization-mode", "walk-forward", "--optimize-params",
            "weights,confirmation")
    assert r.returncode == 2 and "WF-010" in r.stderr
    r = cli("rebalance-scan", "--band-pp", "1,5")
    assert r.returncode == 2 and "ALLOC-001" in r.stderr
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


def test_cli_008_data_file_override(tmp_path):
    """CLI-008 / Q-025 / DATA-008: the exact command (no --start) on the fixture moj_spx.csv:
    the first return week is the earliest week of the common return range with the complete
    signal warm-up (no hard-coded default), the common 1933-03-10 market closure is reported and
    skipped (Q-012), the override is recorded in config_resolved.yaml and data_manifest.json
    with its SHA-256, and summary / weekly / standard metrics are written."""
    staged = WORK.parent / "input" / "data" / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"
    fixture = tmp_path / "moj_spx.csv"
    fixture.write_bytes(staged.read_bytes())
    r = cli("run", "--data-file", "stocks_price=moj_spx.csv", "--weights", "stocks=1.0",
            cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = only_dir(tmp_path / "results")
    for name in STANDARD:
        assert (out / name).is_file(), name
    m = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    assert m["data_overrides"] == {"stocks_price": {
        "config_key": "data.overrides.stocks_price", "path": "moj_spx.csv",
        "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}}
    assert m["first_return_week"] == "1926-07-02" and "Q-025" in m["first_week_rule"]
    assert m["common_calendar_gaps"] == ["1933-03-10"]
    resolved = yaml.safe_load((out / "config_resolved.yaml").read_text())
    assert resolved["data"]["overrides"] == {"stocks_price": "moj_spx.csv"}
    assert resolved["run"]["start"] is None
    s = read_csv(out / "summary.csv")[0]
    assert s["effective_first_week"] == "1926-07-02" and s["requested_start"] == ""
    for k in ("cagr", "after_tax_cagr", "volatility", "sharpe", "max_drawdown", "turnover",
              "final_wealth_pre_tax"):
        assert s[k] != "", k
    weekly = read_csv(out / "weekly_portfolio.csv")
    assert weekly[0]["week_key"] == "1926-07-02" and "1933-03-10" not in {w["week_key"] for w in weekly}


def test_data_file_keys_validated(tmp_path):
    """DATA-008 / Q-025: the keys stocks_price, stocks_return, gold, btc, dividend, cpi (with
    an optional _file suffix); an unknown key or the same source twice in one command is a
    ConfigError before any data is read (never a silent last-wins)."""
    from src.cli import resolve
    from src.errors import ConfigError
    cfg = resolve(["run", "--data-file", "stocks_price=a.csv", "--data-file", "gold_file=g.csv",
                   "--data-file", "cpi=c.csv"])
    assert cfg.get("data.overrides") == {"stocks_price": "a.csv", "gold": "g.csv", "cpi": "c.csv"}
    for argv, msg in (
            (["--data-file", "foo=bar.csv"], "unknown source key 'foo'"),
            (["--data-file", "stocks_price=a.csv", "--data-file", "stocks_price=b.csv"], "twice"),
            (["--data-file", "gold=a.csv", "--data-file", "gold_file=b.csv"], "twice"),
            (["--data-file", "gold=a.csv", "--gold-file", "b.csv"], "twice"),
            (["--data-file", "gold"], "KEY=PATH")):
        with pytest.raises(ConfigError, match=msg):
            resolve(["run", "--weights", "stocks=1", *argv])
    conf = tmp_path / "c.yaml"
    conf.write_text("data:\n  overrides:\n    spx: x.csv\n", encoding="utf-8")
    r = cli("run", "--weights", "stocks=1", "--config", str(conf), cwd=tmp_path)
    assert r.returncode == 2 and "unknown source key 'spx'" in r.stderr
    r = cli("run", "--weights", "stocks=1", "--data-file", "stocks_price=a.csv", "--data-file",
            "stocks_price=b.csv", cwd=tmp_path)
    assert r.returncode == 2 and "given twice" in r.stderr and not (tmp_path / "results").exists()


def test_missing_data_file_error(tmp_path):
    """ERR-001 through the CLI."""
    r = cli("signals", "--asset", "gold", "--gold-file", str(tmp_path / "none.csv"))
    assert r.returncode == 1 and "data.gold_file" in r.stderr and "none.csv" in r.stderr


def test_cli_009_band_rebalance(tmp_path):
    """CLI-009 / S07: the exact band 1 pp command runs end to end through the CLI: band
    rebalance events (1 pp trigger), summary.csv and every standard output. The staged
    gold/stocks proxies stay reported as provenance warnings (Q-002/Q-004 DATA BLOCKER, separate
    rows SEM-001/SEM-003); this test proves the command mechanics."""
    r = cli("run", "--weights", "stocks=0.6,gold=0.2,btc=0.2", "--rebalance", "band",
            "--rebalance-band-pp", "1", "--start", "2018-01-01", "--end", "2026-07-31",
            "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = only_dir(tmp_path)
    for name in STANDARD:
        assert (out / name).is_file(), name
    resolved = yaml.safe_load((out / "config_resolved.yaml").read_text())
    assert resolved["portfolio"]["rebalance"] == "band"
    assert resolved["portfolio"]["rebalance_band_pp"] == 1
    events = read_csv(out / "rebalance_events.csv")
    assert len(events) > 10 and {(e["mode"], e["reason"]) for e in events} == {("band", "band_rebalance")}
    assert all(float(e["max_deviation"]) >= 0.01 - 1e-12 for e in events)
    assert "band_rebalance" in (out / "trades.csv").read_text()
    s = read_csv(out / "summary.csv")[0]
    assert (s["rebalance_mode"], s["rebalance_band_pp"]) == ("band", "1.0")
    assert (s["effective_first_week"], s["effective_last_week"]) == ("2018-01-05", "2026-07-31")
    assert s["cagr"] and s["max_drawdown"] and int(s["trade_count"]) > 0
    prov = {(x["role"], x["code"]) for x in read_csv(out / "validation_report.csv")
            if x["code"] == "provenance"}
    assert {("gold", "provenance"), ("stocks_price", "provenance")} <= prov
    report = (out / "validation_report.csv").read_text()
    assert "Q-002" in report and "SEM-001" in report


def test_cli_individual_pl_run(tmp_path):
    """--tax-profile individual_pl runs end-to-end and writes the tax audit files."""
    r = cli("run", "--weights", "stocks=0.6,gold=0.2,btc=0.2", "--tax-profile", "individual_pl",
            "--start", "2018-01-01", "--end", "2019-12-31", "--as-of-date", "2026-09-29",
            "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    for name in ("tax_events.csv", "realizations.csv", "dividend_reinvestments.csv", "tax_state.json"):
        assert (out / name).is_file(), name
    assert "capital_gains_tax" in (out / "tax_events.csv").read_text()
