"""Funding of amounts due without a strategic rebalance trigger: sell_to_pay (TAX-006, Q-046,
PORT-011 step 3, REB-011).

Deterministic waterfall, independent of the order of asset keys (canonical asset order and
exactly rounded math.fsum sums):
  (A) rf_base, no cost;
  (B) RF reserves pro rata to their current values, no cost, signal state unchanged;
  (C) stocks/gold/btc pro rata to current market values, gross = N/(1-c) so that the net
      proceeds cover the shortfall N exactly; each sale pays costs/slippage, updates the cost
      basis and records a realization; reason sell_to_pay; signal state unchanged;
  (D) InsolvencyError when the net liquidation value cannot cover the amount.
No component ever becomes negative. Payments are Payment records (not trades, not taxes).
"""
from __future__ import annotations

import math

from .engine import AmountsDue, WeekContext
from .errors import InsolvencyError
from .models import RISKY_ASSETS, TradeReason, canonical_assets
from .rf import RF_BASE, reserve_name


class DueCursor:
    """Pays (event_type, amount) items sequentially from funding chunks, one Payment per
    (item, source) piece, in the order the chunks are offered."""

    def __init__(self, pf, week, due: AmountsDue, step=3, phase: str = "weekly"):
        self.pf, self.week, self.step, self.phase = pf, week, step, phase
        self.items = [[t, a] for t, a in due.normalized_items()]

    @property
    def remaining(self) -> float:
        return math.fsum(a for _, a in self.items)

    def pay(self, source: str, amount: float, context: str) -> float:
        paid_total = 0.0
        while amount > 0 and self.items:
            event_type, left = self.items[0]
            p = self.pf.pay(self.week, min(left, amount), event_type, source, context, self.step,
                            self.phase)
            paid = p.amount if p else 0.0
            if paid <= 0:
                break
            paid_total += paid
            amount -= paid
            self.items[0][1] = left - paid
            if self.items[0][1] <= 1e-12 * max(1.0, left):
                self.items.pop(0)
        return paid_total


def sell_to_pay(pf, ctx: WeekContext, due: AmountsDue, step=3, phase: str = "weekly",
                reason=None) -> None:
    """TAX-006 without a strategic trigger, in this exact order:
      (A) rf_base, no cost;
      (B) RF reserves pro rata to their current values, no cost;
      (C) stocks/gold/btc pro rata to current market values, gross = N/(1-c) (Q-046), each
          sale with costs, cost-basis update and realization, reason sell_to_pay;
      (D) insolvency error when the net liquidation value cannot cover the amount.
    Signal states are never changed. ``step``/``phase`` mark the trades and payments (weekly
    step 3 by default; the pre-tax foundation cost settlement uses phase 'terminal').
    ``reason`` - the TradeReason of the step-C sales: the caller's, else ``due.sale_reason``
    (scheduled foundation distributions: foundation_distribution_liquidation), else
    sell_to_pay."""
    week = ctx.week
    reason = reason or due.sale_reason or TradeReason.SELL_TO_PAY
    cursor = DueCursor(pf, week, due, step, phase)
    if not cursor.items:
        return
    led = pf.ledger
    c = pf.costs.tc_rate + pf.costs.slip_rate
    assets = canonical_assets(RISKY_ASSETS)
    reserves = {a: led.reserve(a) for a in assets if led.reserve(a) > 0}
    values = {a: led.asset(a) for a in assets if led.asset(a) > 0}
    net_liq = math.fsum([led.rf_base] + list(reserves.values())) + (1.0 - c) * math.fsum(values.values())
    if due.total > net_liq * (1 + 1e-12):
        raise InsolvencyError(week, due.total, net_liq)                    # (D)
    cursor.pay(RF_BASE, min(due.total, led.rf_base), "sell_to_pay:A_rf_base")       # (A)
    left = cursor.remaining
    if left > 0 and reserves:                                               # (B)
        r_tot = math.fsum(reserves.values())
        take = min(left, r_tot)
        parts = {a: (reserves[a] if take >= r_tot else take * reserves[a] / r_tot) for a in reserves}
        for a in assets:
            if a in parts:
                cursor.pay(reserve_name(a), parts[a], "sell_to_pay:B_reserves_pro_rata")
        left = cursor.remaining
    tol = 1e-9 * max(1.0, due.total)
    if left > tol and values:                                               # (C)
        v_tot = math.fsum(values.values())
        gross = min(left / (1.0 - c), v_tot)
        cash_before = pf.ledger.rf_base
        for a in assets:
            if a in values:
                g = values[a] if gross >= v_tot else gross * values[a] / v_tot
                pf.sell(week, a, g, reason, RF_BASE, step=step, phase=phase)
        raised = pf.ledger.rf_base - cash_before
        cursor.pay(RF_BASE, min(left, raised), "sell_to_pay:C_risky_assets_pro_rata")
