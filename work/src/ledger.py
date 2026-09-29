"""Portfolio ledger (PORT-005..007, PORT-009, RISK-002, RISK-004, Q-049).

Explicit components:
    stocks, gold, btc, rf_base, rf_reserve_stocks, rf_reserve_gold, rf_reserve_btc
NAV is always constructed from the components with math.fsum in this fixed order, and the
strategic sleeve of a risky asset is asset + its dedicated RF reserve.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from .models import RISKY_ASSETS, TOL_IDENTITY_REL, PortfolioSleeves
from .rf import RF_BASE, reserve_name

COMPONENTS = RISKY_ASSETS + (RF_BASE,) + tuple(reserve_name(a) for a in RISKY_ASSETS)


class LedgerInvariantError(AssertionError):
    pass


@dataclass(frozen=True)
class Ledger:
    stocks: float = 0.0
    gold: float = 0.0
    btc: float = 0.0
    rf_base: float = 0.0
    rf_reserve_stocks: float = 0.0
    rf_reserve_gold: float = 0.0
    rf_reserve_btc: float = 0.0

    # ---------------------------------------------------------------- accessors
    def components(self) -> dict:
        return {c: getattr(self, c) for c in COMPONENTS}

    def asset(self, asset: str) -> float:
        return getattr(self, asset)

    def reserve(self, asset: str) -> float:
        return getattr(self, reserve_name(asset))

    def sleeve(self, asset: str) -> float:
        """PORT-009: sleeve = asset value + its RF reserve."""
        return self.asset(asset) + self.reserve(asset)

    @property
    def nav(self) -> float:
        return math.fsum(getattr(self, c) for c in COMPONENTS)

    def sleeve_weights(self) -> dict:
        """Actual strategic sleeve weights (targets apply to sleeve totals, PORT-010)."""
        nav = self.nav
        out = {a: self.sleeve(a) / nav for a in RISKY_ASSETS}
        out["rf"] = self.rf_base / nav
        return out

    def replace(self, **changes) -> "Ledger":
        return dataclasses.replace(self, **changes)

    # ---------------------------------------------------------------- invariants
    def check(self, context: str = "") -> None:
        """RISK-004/Q-049 plus finiteness and non-negativity of every component."""
        comps = self.components()
        for name, v in comps.items():
            if not math.isfinite(v):
                raise LedgerInvariantError(f"{context}{name} is not finite: {v!r}")
            if v < 0:
                raise LedgerInvariantError(f"{context}{name} is negative: {v!r}")
        nav = self.nav
        parts = math.fsum(self.sleeve(a) for a in RISKY_ASSETS) + self.rf_base
        if abs(nav - parts) / max(1.0, abs(nav)) > TOL_IDENTITY_REL:
            raise LedgerInvariantError(f"{context}identity broken: nav={nav!r} sleeves+rf_base={parts!r}")

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_sleeves(cls, ps: PortfolioSleeves) -> "Ledger":
        kw = {"rf_base": ps.rf_base}
        for s in ps.risky:
            kw[s.asset] = s.asset_value
            kw[reserve_name(s.asset)] = s.reserve_value
        return cls(**kw)
