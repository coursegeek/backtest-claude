import csv
import json

import pytest

from fixtures.builders import REPO
from src.cli import resolve
from src.config import ResolvedConfig, get_path, parse_grid, parse_weights
from src.errors import ConfigError

SPEC = REPO / "input" / "python_backtest_specification_v3_1.csv"
DEF_ROWS = [r for r in csv.DictReader(SPEC.open(encoding="utf-8-sig")) if r["section"] == "26_DEFAULTS"]


def _spec_value(text):
    try:
        return float(text)
    except ValueError:
        return text


@pytest.mark.parametrize("row", DEF_ROWS, ids=[r["requirement_id"] for r in DEF_ROWS])
def test_default_values(row):
    """DEF-001..DEF-033: every documented default is the built-in default."""
    cfg = ResolvedConfig()
    got = cfg.get(row["config_key"])
    want = _spec_value(row["default_value"])
    if row["requirement_id"] == "DEF-029":
        assert got is None and want == "required"
    elif isinstance(want, float):
        assert float(got) == want
    else:
        assert got == want
    assert cfg.source_of(row["config_key"]) in ("defaults", "unset")


def test_spec_version_recorded():
    """META-000."""
    cfg = ResolvedConfig()
    assert cfg.get("app.spec_version") == "3.1" and "spec_version: '3.1'" in cfg.to_yaml()


def test_precedence_cli_over_file_over_defaults(tmp_path):
    """META-004: CLI > config file > defaults, with per-key provenance."""
    f = tmp_path / "c.yaml"
    f.write_text("portfolio:\n  initial_capital_pln: 500000\n  slippage_bps: 3\n", encoding="utf-8")
    cfg = resolve(["signals", "--config", str(f), "--initial-capital", "250000"])
    assert cfg.get("portfolio.initial_capital_pln") == 250000.0
    assert cfg.get("portfolio.slippage_bps") == 3
    assert cfg.get("portfolio.transaction_cost_bps") == 0.0
    assert (cfg.source_of("portfolio.initial_capital_pln"), cfg.source_of("portfolio.slippage_bps"),
            cfg.source_of("portfolio.transaction_cost_bps")) == ("cli", "file", "defaults")


def test_yaml_and_json_equivalent(tmp_path):
    """META-005: YAML and JSON config files resolve identically."""
    data = {"run": {"start": "2018-01-01"}, "signals": {"btc": {"threshold": 0.05}}}
    (tmp_path / "c.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "c.yaml").write_text("run:\n  start: 2018-01-01\nsignals:\n  btc:\n    threshold: 0.05\n",
                                     encoding="utf-8")
    a = resolve(["signals", "--config", str(tmp_path / "c.json")])
    b = resolve(["signals", "--config", str(tmp_path / "c.yaml")])
    assert a.data == b.data and a.signal_params("btc").threshold_off == 0.05


def test_frequency_only_weekly():
    with pytest.raises(ConfigError):
        ResolvedConfig(cli_layer={"engine": {"frequency": "daily"}})


def test_currency_pln_only():
    """META-007, RUN-004."""
    with pytest.raises(ConfigError, match="RUN-004"):
        ResolvedConfig(cli_layer={"portfolio": {"currency": "USD"}})


def test_no_fx_inputs_or_conversions():
    """DATA-006, SCHEMA-006: no FX input exists and enabling one is a configuration error."""
    assert "fx_file" not in ResolvedConfig().get("data")
    with pytest.raises(ConfigError):
        ResolvedConfig(file_layer={"data": {"ignore_fx": False}})


def test_initial_capital_positive():
    """RUN-003."""
    assert ResolvedConfig().get("portfolio.initial_capital_pln") == 1_000_000.0
    with pytest.raises(ConfigError):
        ResolvedConfig(cli_layer={"portfolio": {"initial_capital_pln": 0}})


def test_command_specific_defaults():
    """Q-027, DELAY-002, THR-002/003."""
    assert ResolvedConfig("delay-scan").get("optimizer.delay_grid") == [1, 2, 3, 4]
    t = ResolvedConfig("threshold-scan")
    assert t.get("optimizer.threshold_grid") == [0.01, 0.02, 0.03, 0.04, 0.05]
    assert t.get("optimizer.delay_grid") == [1]


def test_cli_units_percent_vs_decimal():
    """Q-026: percent CLI thresholds/weight grids become decimals; --weights stay decimal."""
    cfg = resolve(["delay-scan", "--asset", "stocks", "--ma", "50", "--threshold", "3",
                   "--confirm-weeks", "2", "--delay", "1:4", "--sell-fraction", "0.5"])
    p = cfg.signal_params("stocks")
    assert (p.ma, p.threshold_off, p.threshold_on, p.confirm_off, p.sell_fraction) == (50, 0.03, 0.03, 2, 0.5)
    assert cfg.get("optimizer.delay_grid") == [1, 2, 3, 4]
    assert cfg.signal_params("gold").threshold_off == 0.0          # only the scanned asset
    t = resolve(["threshold-scan", "--asset", "stocks", "--threshold", "1:5:1", "--delay", "1"])
    assert t.get("optimizer.threshold_grid") == [0.01, 0.02, 0.03, 0.04, 0.05]
    o = resolve(["optimize", "--btc-weight", "0:25:1", "--gold-weight", "0:10:5"])
    assert o.get("optimizer.gold_weight") == [0.0, 0.05, 0.1] and len(o.get("optimizer.btc_weight")) == 26
    r = resolve(["run", "--weights", "stocks=0.4,gold=0.4,rf=0.2", "--ma", "40"])
    assert all(r.signal_params(a).ma == 40 for a in ("stocks", "gold", "btc"))


def test_signal_lookup_layer_then_specificity(tmp_path):
    """Q-027: a file-level global signal value beats a built-in per-asset default; within a
    layer the per-asset key beats the global one."""
    cfg = ResolvedConfig(file_layer={"signal": {"threshold": 0.02},
                                     "signals": {"gold": {"threshold": 0.01}}})
    assert cfg.signal_params("stocks").threshold_off == 0.02
    assert cfg.signal_params("gold").threshold_off == 0.01
    asym = ResolvedConfig(cli_layer={"signals": {"btc": {"threshold_off": 0.01, "threshold_on": 0.05}}})
    p = asym.signal_params("btc")
    assert (p.threshold_off, p.threshold_on) == (0.01, 0.05)         # SIG-007
    sep = ResolvedConfig(cli_layer={"signals": {"stocks": {"confirm_off_weeks": 3}}})
    assert (sep.signal_params("stocks").confirm_off, sep.signal_params("stocks").confirm_on) == (3, 2)


def test_parse_grid_and_weights():
    """DELAY-003, THR-005, ALLOC-001."""
    assert parse_grid("1,2,4,8") == [1, 2, 4, 8]
    assert parse_grid("0,1,2,3,5,7.5") == [0, 1, 2, 3, 5, 7.5]
    assert parse_grid("1:4") == [1, 2, 3, 4]
    for bad in ("", "5:1", "1:x", "1:2:0"):
        with pytest.raises(ConfigError):
            parse_grid(bad)
    assert parse_weights("stocks=0.4,gold=0.4,rf=0.2") == {"stocks": 0.4, "gold": 0.4, "btc": 0.0, "rf": 0.2}
    for bad in ("stocks=0.5,gold=0.4", "stocks=1.2", "eth=1", "stocks=0.5,stocks=0.5", "stocks"):
        with pytest.raises(ConfigError):
            parse_weights(bad)


def test_invalid_signal_parameters_rejected():
    for layer in ({"signal": {"ma_length": 1}}, {"signals": {"stocks": {"confirm_weeks": 0}}},
                  {"signal": {"delay_weeks": 0}}, {"signals": {"btc": {"threshold": -0.01}}},
                  {"signals": {"gold": {"sell_fraction": 1.5}}},
                  {"signals": {"gold": {"risk_off_action": "sell_all"}}}):
        with pytest.raises(ConfigError):
            ResolvedConfig(file_layer=layer)


def test_resolved_yaml_roundtrip_and_as_of():
    """REP-007, REPRO-007: the resolved config is complete, sorted and records as_of_date."""
    import datetime as dt
    import yaml
    cfg = ResolvedConfig()
    text = cfg.to_yaml(dt.date(2026, 9, 29))
    data = yaml.safe_load(text)
    assert get_path(data, "run.as_of_date") == "2026-09-29"
    assert data["config"]["precedence"] == "CLI > config > defaults"
    assert text == cfg.to_yaml(dt.date(2026, 9, 29))
