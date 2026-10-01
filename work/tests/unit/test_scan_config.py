"""Scan configuration and grids without data (Q-026 units, Q-027 precedence, ALLOC-001/002,
DELAY-002/003, THR-002/003/005, REB-010): the whole grid and every variant configuration are
resolved and validated before any source is loaded."""
import copy
from decimal import Decimal

import pytest

from src import scans
from src.cli import resolve
from src.config import (ResolvedConfig, int_grid, parse_decimal_grid, parse_grid, percent_grid,
                        percent_value)
from src.errors import ConfigError, NotImplementedCommand


# ------------------------------------------------------------------ Q-026 units / grid syntax
def test_range_and_list_syntax_is_exact_and_inclusive():
    assert int_grid("1:4") == [1, 2, 3, 4]                          # DELAY-002 default
    assert int_grid("1,2,4,8") == [1, 2, 4, 8]                      # DELAY-003
    assert int_grid("1:3,8") == [1, 2, 3, 8]
    assert parse_grid("1:5:1") == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert parse_grid("0:1:0.1")[3] == 0.3                           # no 0.30000000000000004
    assert parse_decimal_grid("0:1:0.25") == [Decimal("0"), Decimal("0.25"), Decimal("0.50"),
                                              Decimal("0.75"), Decimal("1.00")]
    assert parse_grid("0:1:0.3") == [0.0, 0.3, 0.6, 0.9]              # end not reached exactly
    assert parse_grid("4,1,2") == [4.0, 1.0, 2.0]                     # order as given
    for bad in ("1:", "5:1", "1:4:0", "1:4:-1", "a", "1,,2", "", "nan", "1:2:3:4"):
        with pytest.raises(ConfigError):
            parse_grid(bad)
    for bad in ("1.5", "0", "-1", "1:2:0.5"):
        with pytest.raises(ConfigError, match="integers"):
            int_grid(bad)


def test_percent_units_are_never_guessed():
    """Q-026: CLI thresholds are percent: 3 -> 0.03, 0.03 -> 0.0003 (0.03%)."""
    assert percent_value("3", "t") == 0.03
    assert percent_value("0.03", "t") == 0.0003
    assert percent_value("7.5", "t") == 0.075
    assert percent_grid("1:5:1") == [0.01, 0.02, 0.03, 0.04, 0.05]  # THR-002
    assert percent_grid("0,1,2,3,5,7.5") == [0.0, 0.01, 0.02, 0.03, 0.05, 0.075]   # THR-005
    assert percent_grid("1.1") == [0.011]                             # decimal, not 1.1/100.0
    with pytest.raises(ConfigError):
        percent_value("1,2", "t")


def test_cli_units():
    c = resolve(["delay-scan", "--asset", "stocks", "--threshold", "0.03"])
    assert c.signal_params("stocks").threshold_off == 0.0003
    c = resolve(["delay-scan", "--asset", "stocks", "--threshold", "3", "--sell-fraction", "0.5"])
    p = c.signal_params("stocks")
    assert (p.threshold_off, p.threshold_on, p.sell_fraction) == (0.03, 0.03, 0.5)
    t = resolve(["threshold-scan", "--asset", "gold", "--threshold", "0,1,2,3,5,7.5"])
    assert t.get("optimizer.threshold_grid") == [0.0, 0.01, 0.02, 0.03, 0.05, 0.075]
    r = resolve(["rebalance-scan", "--weights", "stocks=0.6,gold=0.2,btc=0.2", "--band-pp", "1,5",
                 "--sortino-mar", "0.0"])
    assert r.get("optimizer.rebalance_band_grid") == [1.0, 5.0]       # percentage points
    assert r.get("allocation.targets") == "stocks=0.6,gold=0.2,btc=0.2"   # decimals
    assert r.get("metrics.sortino_mar_annual") == 0.0
    run = resolve(["run", "--weights", "stocks=1", "--rebalance", "band", "--rebalance-band-pp", "1"])
    assert run.get("portfolio.rebalance_band_pp") == 1.0
    for argv, msg in ((["delay-scan", "--asset", "stocks", "--delay", "1.5"], "integers"),
                      (["delay-scan", "--asset", "stocks", "--ma", "50.5"], "integers"),
                      (["threshold-scan", "--asset", "stocks", "--delay", "1:2"], "single"),
                      (["threshold-scan", "--asset", "stocks", "--threshold-off", "2"], "symmetric"),
                      (["threshold-scan", "--asset", "stocks", "--threshold-grid", "1,2"], "symmetric")):
        with pytest.raises(ConfigError, match=msg):
            resolve(argv)


def test_config_file_thresholds_are_decimal(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("optimizer:\n  threshold_grid: [0.0, 0.01, 0.075]\n", encoding="utf-8")
    spec = scans.resolve_scan(resolve(["threshold-scan", "--asset", "stocks", "--config", str(f)]))
    assert spec.resolved_grid == [0.0, 0.01, 0.075]
    f.write_text("optimizer:\n  threshold_grid: [3]\n", encoding="utf-8")      # not percent
    with pytest.raises(ConfigError, match="decimal fractions"):
        scans.resolve_scan(resolve(["threshold-scan", "--asset", "stocks", "--config", str(f)]))


# ------------------------------------------------------------------ Q-027 precedence
def test_q027_parameter_precedence(tmp_path):
    """CLI > signals.<asset>.* > signal.* > signals.default.* > DEF-*; scan flags only for
    --asset."""
    f = tmp_path / "c.yaml"
    f.write_text("signal:\n  ma_length: 40\n  threshold: 0.05\n"
                 "signals:\n  default:\n    sell_fraction: 0.3\n    confirm_weeks: 3\n"
                 "  stocks:\n    ma_length: 45\n", encoding="utf-8")
    c = resolve(["delay-scan", "--asset", "stocks", "--config", str(f)])
    s, g = c.signal_params("stocks"), c.signal_params("gold")
    assert s.ma == 45 and g.ma == 40                         # signals.<asset> > signal.*
    assert s.threshold_off == 0.05                           # signal.* > DEF signals.stocks (0)
    assert s.sell_fraction == 0.3                            # signals.default > DEF (0.5)
    assert s.confirm_off == 3 and g.confirm_off == 3         # file default beats DEF per asset
    c = resolve(["delay-scan", "--asset", "stocks", "--config", str(f), "--ma", "60",
                 "--confirm-weeks", "2"])
    assert c.signal_params("stocks").ma == 60 and c.signal_params("gold").ma == 40   # --asset only
    assert c.signal_params("stocks").confirm_off == 2 and c.signal_params("gold").confirm_off == 3
    d = ResolvedConfig("run")
    assert (d.signal_params("stocks").delay, d.signal_params("stocks").confirm_off) == (1, 2)
    assert ResolvedConfig("delay-scan").get("optimizer.delay_grid") == [1, 2, 3, 4]
    assert ResolvedConfig("threshold-scan").get("optimizer.threshold_grid") == [
        0.01, 0.02, 0.03, 0.04, 0.05]


# ------------------------------------------------------------------ scan resolution
def test_delay_scan_is_single_asset_and_only_delay_changes():
    base = resolve(["delay-scan", "--asset", "btc", "--delay", "1,2,4,8", "--ma", "30"])
    before = copy.deepcopy(base.data), copy.deepcopy(base.layers)
    spec = scans.resolve_scan(base)
    assert (base.data, base.layers) == before                          # never mutated
    assert spec.asset == "btc" and spec.resolved_grid == [1, 2, 4, 8]
    assert spec.base.get("allocation.single_asset") == "btc"
    from src.allocation import strategic_targets
    assert strategic_targets(spec.base) == {"stocks": 0.0, "gold": 0.0, "btc": 1.0, "rf": 0.0}
    ps = [c.signal_params("btc") for c in spec.configs]
    assert [p.delay for p in ps] == [1, 2, 4, 8]
    assert len({(p.ma, p.threshold_off, p.threshold_on, p.confirm_off, p.confirm_on,
                 p.sell_fraction, p.risk_off_action) for p in ps}) == 1
    assert spec.warmup_params["btc"].delay == 8                        # largest warm-up
    assert [p.grid_index for p in spec.points] == [1, 2, 3, 4]
    assert spec.parameter == "signals.btc.delay_weeks"


def test_single_asset_scans_reject_hidden_weights():
    with pytest.raises(ConfigError, match="ALLOC-002"):
        scans.resolve_scan(resolve(["delay-scan"]))
    with pytest.raises(ConfigError, match="ALLOC-002"):
        scans.resolve_scan(resolve(["threshold-scan", "--asset", "stocks",
                                    "--weights", "stocks=0.5,rf=0.5"]))
    with pytest.raises(ConfigError, match="conflicts"):
        scans.resolve_scan(resolve(["delay-scan", "--asset", "stocks", "--single-asset", "gold"]))


def test_threshold_scan_symmetric_and_scalar_delay(tmp_path):
    """THR-002: both sides of the band come from the grid value, even when the configuration
    has asymmetric thresholds; THR-003: the scalar delay (default 1), never a delay grid."""
    f = tmp_path / "c.yaml"
    f.write_text("signals:\n  stocks:\n    threshold_off: 0.07\n    threshold_on: 0.01\n"
                 "optimizer:\n  delay_grid: [1, 2, 3, 4]\n", encoding="utf-8")
    spec = scans.resolve_scan(resolve(["threshold-scan", "--asset", "stocks", "--config", str(f),
                                       "--threshold", "0,1,7.5"]))
    ps = [c.signal_params("stocks") for c in spec.configs]
    assert [(p.threshold_off, p.threshold_on) for p in ps] == [(0.0, 0.0), (0.01, 0.01),
                                                               (0.075, 0.075)]
    assert {p.delay for p in ps} == {1}
    spec = scans.resolve_scan(resolve(["threshold-scan", "--asset", "stocks", "--delay", "2"]))
    assert {c.signal_params("stocks").delay for c in spec.configs} == {2}
    assert spec.resolved_grid == [0.01, 0.02, 0.03, 0.04, 0.05]


def test_rebalance_scan_forces_band_and_requires_weights():
    spec = scans.resolve_scan(resolve(["rebalance-scan", "--weights", "stocks=0.6,gold=0.2,btc=0.2",
                                       "--band-pp", "5,1", "--rebalance", "quarterly"]))
    assert spec.resolved_grid == [5.0, 1.0]                            # grid order as given
    assert [(c.get("portfolio.rebalance"), c.get("portfolio.rebalance_band_pp"))
            for c in spec.configs] == [("band", 5.0), ("band", 1.0)]
    with pytest.raises(ConfigError, match="ALLOC-001"):
        scans.resolve_scan(resolve(["rebalance-scan", "--band-pp", "1,5"]))
    with pytest.raises(ConfigError, match="--band-pp"):
        scans.resolve_scan(resolve(["rebalance-scan", "--weights", "stocks=1"]))
    with pytest.raises(ConfigError, match="--asset is not used"):
        scans.resolve_scan(resolve(["rebalance-scan", "--weights", "stocks=1", "--asset", "stocks",
                                    "--band-pp", "1"]))


@pytest.mark.parametrize("argv,msg", [
    (["delay-scan", "--asset", "stocks", "--delay", "1,2,1"], "duplicate"),
    (["threshold-scan", "--asset", "stocks", "--threshold", "1,1"], "duplicate"),
    (["threshold-scan", "--asset", "stocks", "--threshold", "150"], "decimal fractions"),
    (["rebalance-scan", "--weights", "stocks=1", "--band-pp", "0,1"], r"\(0, 100\]"),
    (["rebalance-scan", "--weights", "stocks=1", "--band-pp", "1,1"], "duplicate"),
])
def test_invalid_grids_fail_before_data(argv, msg):
    with pytest.raises(ConfigError, match=msg):
        scans.resolve_scan(resolve(argv))


def test_foundation_constraints_stop_the_scan_before_data():
    """Foundation configuration errors (a schedule without its file, Q-037) stop the scan at
    its first grid point before any data (never bypassed for a grid)."""
    with pytest.raises(scans.ScanError) as e:
        scans.resolve_scan(resolve(["delay-scan", "--asset", "stocks", "--tax-profile",
                                    "family_foundation_15", "--foundation-tax-event",
                                    "distribution_schedule"]))
    assert isinstance(e.value.cause, ConfigError) and "Q-037" in str(e.value)
    assert e.value.point.grid_index == 1 and e.value.exit_code == 2
