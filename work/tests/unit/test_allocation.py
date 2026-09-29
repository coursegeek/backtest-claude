import pytest

from src.allocation import (active_risky_assets, initial_sleeves, scale_sleeve,
                            single_asset_targets, strategic_targets)
from src.config import ResolvedConfig
from src.errors import ConfigError
from src.models import SignalParams, Sleeve, State

P = {a: SignalParams(a, 50, 0, 0, 1, 1, 1, 0.5, "sell_fraction_current") for a in ("stocks", "gold", "btc")}


def test_run_requires_targets():
    """ALLOC-001, DEF-029."""
    with pytest.raises(ConfigError, match="ALLOC-001"):
        strategic_targets(ResolvedConfig("run"))
    t = strategic_targets(ResolvedConfig("run", cli_layer={"allocation": {"targets": "stocks=0.4,gold=0.4,rf=0.2"}}))
    assert t == {"stocks": 0.4, "gold": 0.4, "btc": 0.0, "rf": 0.2}
    with pytest.raises(ConfigError):
        strategic_targets(ResolvedConfig("run", cli_layer={"allocation": {"targets": "stocks=1",
                                                                          "single_asset": "gold"}}))


def test_single_asset_mode():
    """ALLOC-003."""
    t = strategic_targets(ResolvedConfig("run", cli_layer={"allocation": {"single_asset": "gold"}}))
    assert t == {"stocks": 0.0, "gold": 1.0, "btc": 0.0, "rf": 0.0}
    with pytest.raises(ConfigError):
        ResolvedConfig("run", cli_layer={"allocation": {"single_asset": "eth"}})


def test_scan_asset_is_single_asset_strategy():
    """ALLOC-002: --asset in scans = 100% sleeve of that asset with RF only as its reserve."""
    assert single_asset_targets("btc") == {"stocks": 0.0, "gold": 0.0, "btc": 1.0, "rf": 0.0}
    assert active_risky_assets({"btc": 0.2, "stocks": 0.6, "gold": 0.0, "rf": 0.2}) == ("stocks", "btc")


def test_initial_split_follows_state():
    """SIG-018, ALLOC-004, PORT-009/010: RISK_OFF sleeves start split, rf_base is separate."""
    t = {"stocks": 0.5, "gold": 0.3, "btc": 0.0, "rf": 0.2}
    ps = initial_sleeves(1000.0, t, {"stocks": State.RISK_OFF, "gold": State.RISK_ON}, P)
    assert (ps.sleeve("stocks").asset_value, ps.sleeve("stocks").reserve_value) == (250.0, 250.0)
    assert (ps.sleeve("gold").asset_value, ps.sleeve("gold").reserve_value) == (300.0, 0.0)
    assert ps.rf_base == 200.0 and ps.nav == 1000.0
    assert ps.weights() == {"stocks": 0.5, "gold": 0.3, "btc": 0.0, "rf": 0.2}
    ps.check_identity(1000.0)


def test_target_fraction_of_sleeve():
    """RISK-008 / Q-042 initial split for target_fraction_of_sleeve."""
    p = {"stocks": SignalParams("stocks", 50, 0, 0, 1, 1, 1, 0.3, "target_fraction_of_sleeve")}
    ps = initial_sleeves(100.0, {"stocks": 1.0, "rf": 0.0}, {"stocks": State.RISK_OFF}, p)
    assert (ps.sleeve("stocks").asset_value, ps.sleeve("stocks").reserve_value) == (70.0, 30.0)


def test_preserve_signal_split():
    """REB-009 helper: resizing keeps the asset/reserve proportion."""
    s = scale_sleeve(Sleeve("stocks", 30.0, 10.0), 80.0)
    assert (s.asset_value, s.reserve_value) == (60.0, 20.0)


def test_sleeve_totals_and_identity_violation():
    from src.models import PortfolioSleeves
    ps = PortfolioSleeves((Sleeve("stocks", 60.0, 40.0),), 0.0)
    assert ps.sleeve("stocks").total == 100.0
    with pytest.raises(AssertionError):
        ps.check_identity(100.01)
