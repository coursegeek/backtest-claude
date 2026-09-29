"""Strategic allocation and sleeve construction (ALLOC-001..004, PORT-009/010, SIG-018,
RISK-008, REB-009). Pure functions; no trading."""
from __future__ import annotations

import math

from .config import parse_weights
from .errors import ConfigError
from .models import RISKY_ASSETS, PortfolioSleeves, Sleeve, State, canonical_assets


def strategic_targets(cfg) -> dict:
    """ALLOC-001/003, DEF-029: explicit targets or explicit single-asset mode; never implicit."""
    single = cfg.get("allocation.single_asset")
    targets = cfg.get("allocation.targets")
    if single not in (None, False) and targets is not None:
        raise ConfigError("use either allocation.targets (--weights) or allocation.single_asset "
                          "(--single-asset), not both")
    if single not in (None, False):
        return single_asset_targets(single)
    if targets is None:
        raise ConfigError("a full portfolio run requires allocation.targets (--weights "
                          "stocks=..,gold=..,btc=..,rf=..) or --single-asset (ALLOC-001, DEF-029)")
    return parse_weights(targets)


def single_asset_targets(asset: str) -> dict:
    """ALLOC-002/003: the tested asset's strategic sleeve is 100%; RF acts only as its risk-off
    reserve (strategic rf weight 0)."""
    if asset not in RISKY_ASSETS:
        raise ConfigError(f"single asset must be one of {RISKY_ASSETS}, got {asset!r}")
    out = {a: 0.0 for a in RISKY_ASSETS + ("rf",)}
    out[asset] = 1.0
    return out


def active_risky_assets(targets: dict) -> tuple:
    return canonical_assets([a for a, w in targets.items() if a != "rf" and w > 0])


def risk_off_asset_share(params) -> float:
    """Asset share of the sleeve in RISK_OFF: (1 - sell_fraction) for sell_fraction_current;
    for target_fraction_of_sleeve the target risk-off split uses the same fraction (Q-042)."""
    return 1.0 - params.sell_fraction


def initial_sleeves(capital: float, targets: dict, effective_states: dict,
                    params_by_asset: dict) -> PortfolioSleeves:
    """SIG-018: split every strategic sleeve according to the reconstructed effective state:
    RISK_ON -> 100% asset; RISK_OFF -> asset (1-sell_fraction), reserve sell_fraction."""
    if not capital > 0:
        raise ConfigError("investable capital must be > 0")
    if abs(math.fsum(targets.values()) - 1.0) > 1e-9:
        raise ConfigError("targets must sum to 1")
    sleeves = []
    for asset in canonical_assets([a for a in targets if a != "rf"]):
        total = capital * targets[asset]
        if effective_states.get(asset, State.RISK_ON) == State.RISK_OFF:
            share = risk_off_asset_share(params_by_asset[asset])
            sleeves.append(Sleeve(asset, total * share, total - total * share))
        else:
            sleeves.append(Sleeve(asset, total, 0.0))
    rf_base = capital - math.fsum(s.total for s in sleeves)
    return PortfolioSleeves(tuple(sleeves), rf_base)


def scale_sleeve(sleeve: Sleeve, new_total: float) -> Sleeve:
    """REB-009: resize a sleeve keeping its internal asset/reserve split."""
    if sleeve.total == 0:
        return Sleeve(sleeve.asset, new_total, 0.0)
    f = new_total / sleeve.total
    return Sleeve(sleeve.asset, sleeve.asset_value * f, new_total - sleeve.asset_value * f)
