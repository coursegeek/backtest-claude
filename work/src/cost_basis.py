"""Tax-neutral cost basis foundation (PORT-008, IND-008, IND-011).

Positions are tracked in units of a per-asset unit price index (1.0 at inception, moved by the
asset's weekly return). Lots record units and PLN cost (purchase cost includes transaction
costs and slippage, i.e. the cash spent). Sales realise gains as net proceeds minus the basis
of the units sold, consumed FIFO (default) or at average cost. No tax is computed here; the
engine only reports realised gains for the future tax module.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

from .errors import ConfigError

METHODS = ("FIFO", "average_cost")


@dataclass(frozen=True)
class Lot:
    lot_id: int
    asset: str
    open_week: dt.date
    units: float
    cost: float
    source: str                     # initial | buy | (later) dividend_reinvest


@dataclass(frozen=True)
class Realization:
    asset: str
    week_key: dt.date
    units_sold: float
    proceeds_net: float
    cost_basis: float
    realized_gain: float
    consumed: tuple                 # ((lot_id, units, cost), ...)


class CostBasisBook:
    def __init__(self, method: str = "FIFO"):
        if method not in METHODS:
            raise ConfigError(f"cost basis method {method!r} not in {METHODS}")
        self.method = method
        self._lots: dict = {}
        self._next_id = 1

    def copy(self) -> "CostBasisBook":
        other = CostBasisBook(self.method)
        other._lots = {a: list(v) for a, v in self._lots.items()}
        other._next_id = self._next_id
        return other

    def lots(self, asset: str) -> tuple:
        return tuple(self._lots.get(asset, ()))

    def units(self, asset: str) -> float:
        return math.fsum(l.units for l in self._lots.get(asset, ()))

    def cost(self, asset: str) -> float:
        return math.fsum(l.cost for l in self._lots.get(asset, ()))

    def open_lot(self, asset: str, week: dt.date, units: float, cost: float, source: str) -> Lot:
        if units < 0 or cost < 0:
            raise ValueError("lot units and cost must be >= 0")
        lot = Lot(self._next_id, asset, week, units, cost, source)
        self._next_id += 1
        if units > 0:
            self._lots.setdefault(asset, []).append(lot)
        return lot

    def sell_fraction(self, asset: str, fraction: float, week: dt.date,
                      proceeds_net: float) -> Realization:
        """Sell ``fraction`` (0..1] of the units held; realised gain = proceeds - basis."""
        if not 0 < fraction <= 1 + 1e-12:
            raise ValueError(f"sell fraction must be in (0, 1], got {fraction!r}")
        lots = self._lots.get(asset, [])
        total = self.units(asset)
        to_sell = total * min(fraction, 1.0)
        consumed, remaining = [], []
        if fraction >= 1.0:
            consumed = [(l.lot_id, l.units, l.cost) for l in lots]
        elif self.method == "FIFO":
            left = to_sell
            for l in lots:
                if left <= 0:
                    remaining.append(l)
                elif l.units <= left:
                    consumed.append((l.lot_id, l.units, l.cost))
                    left -= l.units
                else:
                    part = l.cost * (left / l.units)
                    consumed.append((l.lot_id, left, part))
                    remaining.append(Lot(l.lot_id, l.asset, l.open_week, l.units - left,
                                         l.cost - part, l.source))
                    left = 0.0
        else:                           # average cost: every lot reduced proportionally
            f = min(fraction, 1.0)
            for l in lots:
                consumed.append((l.lot_id, l.units * f, l.cost * f))
                remaining.append(Lot(l.lot_id, l.asset, l.open_week, l.units * (1 - f),
                                     l.cost * (1 - f), l.source))
        self._lots[asset] = remaining
        basis = math.fsum(c for _, _, c in consumed)
        return Realization(asset, week, to_sell, proceeds_net, basis, proceeds_net - basis,
                           tuple(consumed))
