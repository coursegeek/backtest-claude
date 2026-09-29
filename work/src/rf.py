"""RF components (ALLOC-004, RISK-002, RISK-006, PORT-002, PORT-013).

rf_base (strategic RF sleeve) and rf_reserve_<asset> (signal reserves) are cash-like ledger
components (Q-017). All of them earn the same weekly RF return; any RF income tax is charged
separately by the immediate-tax hook of the weekly pipeline (step 5), so the gross return is
applied here and R_rf_net == R_rf for tax.profile=none.
"""
from __future__ import annotations

from .models import RISKY_ASSETS

RF_BASE = "rf_base"


def reserve_name(asset: str) -> str:
    return f"rf_reserve_{asset}"


RF_COMPONENTS = (RF_BASE,) + tuple(reserve_name(a) for a in RISKY_ASSETS)


def grow(value: float, rf_return: float) -> float:
    return value * (1.0 + rf_return)
