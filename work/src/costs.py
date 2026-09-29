"""Transaction costs and slippage (REB-003, REB-004, REB-011, Q-017).

Both apply to every real purchase or sale of stocks, gold or BTC; moving value into or out
of an RF component (rf_base, rf_reserve_<asset>) is not a trade and costs nothing.
  every trade: cost = traded_value*transaction_cost_bps/10000,
               slippage = traded_value*slippage_bps/10000
  sale:        net_proceeds = traded_value * (1 - tc_rate - slip_rate)
  purchase:    cash C buys traded_value = C / (1 + tc_rate + slip_rate)
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError


@dataclass(frozen=True)
class CostModel:
    transaction_cost_bps: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self):
        if self.transaction_cost_bps < 0 or self.slippage_bps < 0:
            raise ConfigError("transaction_cost_bps and slippage_bps must be >= 0")

    @property
    def tc_rate(self) -> float:
        return self.transaction_cost_bps / 10000.0

    @property
    def slip_rate(self) -> float:
        return self.slippage_bps / 10000.0

    def sale(self, traded_value: float):
        """Returns (transaction_cost, slippage, net_proceeds)."""
        cost, slip = traded_value * self.tc_rate, traded_value * self.slip_rate
        return cost, slip, traded_value * (1.0 - self.tc_rate - self.slip_rate)

    def purchase_from_cash(self, cash: float):
        """Largest purchase financed by ``cash``. Returns (traded_value, cost, slippage);
        traded_value + cost + slippage == cash up to rounding."""
        traded = cash / (1.0 + self.tc_rate + self.slip_rate)
        return traded, traded * self.tc_rate, traded * self.slip_rate

    @classmethod
    def from_config(cls, cfg) -> "CostModel":
        return cls(float(cfg.get("portfolio.transaction_cost_bps")),
                   float(cfg.get("portfolio.slippage_bps")))
