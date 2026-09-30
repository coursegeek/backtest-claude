"""Terminal settlement of the individual_pl profile (IND-016, IND-017, IND-020, PORT-014,
REB-011, Q-032 individual_pl part).

A separate layer after the weekly PORT-011 path, never another backtest week:

    weekly path ends (last retained Friday, pre_terminal_nav)
      -> terminal liquidation of 100% stocks/gold/btc (canonical order, reason
         terminal_liquidation, phase terminal): transaction costs + slippage, FIFO/average
         cost basis, realizations; net proceeds to rf_base (RF components are cash-like and
         are not traded)
      -> realizations added to the final (still open) tax year
      -> final-year netting + loss carry-forward, CG tax, solidarity tax - the same
         tax.close_tax_year() as the annual settlement, with settlement='terminal'
      -> RF reserves consolidated into rf_base by cost-free transfers, terminal taxes paid
         from rf_base (InsolvencyError if the cash does not cover them; never negative)
      -> after_tax_terminal_wealth

Everything works on copies: a WorkingPortfolio rebuilt from the engine's final
PortfolioSnapshot and deep copies of the tax state. The weekly records, trades, payments and
tax events of the run are never modified, so terminal costs and taxes never enter the weekly
NAV path, its returns or any drawdown computed from it.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
from dataclasses import dataclass

from .engine import PortfolioSnapshot, WorkingPortfolio
from .errors import InsolvencyError
from .ledger import Ledger
from .models import RISKY_ASSETS, TradeReason, canonical_assets
from .rf import RF_BASE, reserve_name
from .engine import AmountsDue, WeekContext
from .foundation import (ADMIN_COST, DISTRIBUTION_TAX, AdminCost, FoundationParams,
                         FoundationState, admin_cost_event, admin_cost_for_year)
from .sell_to_pay import sell_to_pay
from .tax import AnnualLiability, TaxEvent, TaxParams, TaxState, close_tax_year, tax_year

PHASE = "terminal"
TERMINAL_TAX_TYPES = ("capital_gains_tax", "solidarity_tax")


@dataclass(frozen=True)
class TerminalSettlementResult:
    last_week: dt.date
    final_tax_year: int
    pre_terminal_ledger: Ledger
    pre_terminal_nav: float
    liquidation_trades: tuple
    terminal_realizations: tuple
    terminal_transaction_costs: float
    terminal_slippage: float
    terminal_trading_costs: float
    nav_after_liquidation: float
    terminal_realized_gain: float           # gains/losses realised by the liquidation
    final_year_realized_gain: float         # whole final tax year incl. the liquidation
    loss_offset: float
    taxable_gain: float
    terminal_capital_gains_tax: float
    terminal_solidarity_tax: float
    terminal_tax_total: float
    terminal_transfers: tuple               # reserve consolidation (cost-free)
    terminal_payments: tuple
    terminal_tax_events: tuple
    after_tax_terminal_wealth: float
    final_cash_ledger: Ledger
    final_liability: AnnualLiability
    tax_state_before_terminal: TaxState     # copy of the weekly (pre-terminal) state
    final_tax_state: TaxState

    def breakout(self) -> dict:
        """REP-017/PORT-014 fields (individual_pl; the foundation tax does not apply)."""
        return {
            "last_week": self.last_week.isoformat(),
            "final_tax_year": self.final_tax_year,
            "pre_terminal_nav": self.pre_terminal_nav,
            "terminal_transaction_costs": self.terminal_transaction_costs,
            "terminal_slippage": self.terminal_slippage,
            "terminal_liquidation_costs": self.terminal_trading_costs,
            "nav_after_liquidation": self.nav_after_liquidation,
            "terminal_realized_gain": self.terminal_realized_gain,
            "final_year_realized_gain": self.final_year_realized_gain,
            "loss_offset": self.loss_offset,
            "taxable_gain": self.taxable_gain,
            "terminal_capital_gains_tax": self.terminal_capital_gains_tax,
            "terminal_solidarity_tax": self.terminal_solidarity_tax,
            "terminal_foundation_tax": None,
            "terminal_tax_total": self.terminal_tax_total,
            "after_tax_terminal_wealth": self.after_tax_terminal_wealth,
            "final_cash_ledger": self.final_cash_ledger.components(),
            "liquidation_trades": len(self.liquidation_trades),
        }


def terminal_liquidation(snapshot: PortfolioSnapshot,
                         reason: TradeReason = TradeReason.TERMINAL_LIQUIDATION) -> WorkingPortfolio:
    """IND-016, FND-005, REB-011: sell 100% of stocks, gold and BTC of a copy of the final
    portfolio in canonical order, with transaction costs and slippage; net proceeds to rf_base."""
    pf = WorkingPortfolio.from_snapshot(snapshot)
    for a in canonical_assets(RISKY_ASSETS):
        v = pf.ledger.asset(a)
        if v > 0:
            pf.sell(snapshot.week_key, a, v, reason, RF_BASE, step=None, phase=PHASE)
    if any(pf.ledger.asset(a) != 0.0 for a in RISKY_ASSETS):
        raise AssertionError("terminal liquidation left a risky position")
    pf.check_holdings("terminal liquidation: ")
    return pf


def settle_terminal(snapshot: PortfolioSnapshot, params: TaxParams,
                    tax_state: TaxState) -> TerminalSettlementResult:
    """IND-016, IND-020: terminal liquidation, final-year settlement and after-tax terminal
    wealth. ``snapshot`` and ``tax_state`` are not modified."""
    week, year = snapshot.week_key, tax_year(snapshot.week_key)
    before = tax_state.copy()
    final = tax_state.copy()
    if final.open_year is None:
        final.open_year = year
    if final.open_year != year:
        raise ValueError(f"open tax year {final.open_year} != year {year} of the last week {week}")
    pf = terminal_liquidation(snapshot)
    after_liq = pf.ledger
    trades = tuple(pf.trades)
    reals = tuple(pf.realizations)
    for r in reals:                                        # Q-029: year of the last week
        final.realizations.setdefault(tax_year(r.week_key), []).append(
            (r.asset, r.realized_gain, r.week_key))
    n_events = len(final.tax_events)
    liab = close_tax_year(final, params, year, week, settlement="terminal")
    final.open_year = None                                 # no open year after the terminal
    total = math.fsum([liab.capital_gains_tax, liab.solidarity_tax])
    for a in canonical_assets(RISKY_ASSETS):               # reserves are cash-like: free move
        pf.transfer(week, reserve_name(a), RF_BASE, pf.ledger.reserve(a),
                    "terminal_consolidation", step=None, phase=PHASE)
    cash = pf.ledger.rf_base
    if total > cash * (1 + 1e-12) + 1e-12:
        raise InsolvencyError(week, total, cash)
    for t, amount in zip(TERMINAL_TAX_TYPES, (liab.capital_gains_tax, liab.solidarity_tax)):
        if amount > 0:
            pf.pay(week, amount, t, RF_BASE, "terminal_settlement", step=None, phase=PHASE)
    final.capital_gains_tax_paid += liab.capital_gains_tax
    final.solidarity_tax_paid += liab.solidarity_tax
    liab = dataclasses.replace(liab, paid_week=week, paid_capital_gains_tax=liab.capital_gains_tax,
                               paid_solidarity_tax=liab.solidarity_tax)
    final.annual_liabilities[year] = liab
    pf.ledger.check("terminal settlement: ")
    return TerminalSettlementResult(
        last_week=week, final_tax_year=year,
        pre_terminal_ledger=snapshot.ledger, pre_terminal_nav=snapshot.ledger.nav,
        liquidation_trades=trades, terminal_realizations=reals,
        terminal_transaction_costs=math.fsum(t.transaction_cost for t in trades),
        terminal_slippage=math.fsum(t.slippage for t in trades),
        terminal_trading_costs=math.fsum([t.transaction_cost + t.slippage for t in trades]),
        nav_after_liquidation=after_liq.nav,
        terminal_realized_gain=math.fsum(r.realized_gain for r in reals),
        final_year_realized_gain=liab.annual_realized, loss_offset=liab.loss_offset,
        taxable_gain=liab.taxable_gain, terminal_capital_gains_tax=liab.capital_gains_tax,
        terminal_solidarity_tax=liab.solidarity_tax, terminal_tax_total=total,
        terminal_transfers=tuple(pf.transfers), terminal_payments=tuple(pf.payments),
        terminal_tax_events=tuple(final.tax_events[n_events:]),
        after_tax_terminal_wealth=pf.ledger.nav, final_cash_ledger=pf.ledger,
        final_liability=liab, tax_state_before_terminal=before, final_tax_state=final)


# ============================================================================ foundation
def _consolidate_reserves(pf, week) -> None:
    for a in canonical_assets(RISKY_ASSETS):               # reserves are cash-like: free move
        pf.transfer(week, reserve_name(a), RF_BASE, pf.ledger.reserve(a),
                    "terminal_consolidation", step=None, phase=PHASE)


def _final_admin_cost(params: FoundationParams, state: FoundationState, inception, week):
    year = tax_year(week)
    if state.open_year is None:
        state.open_year = inception.year
    if state.open_year != year:
        raise ValueError(f"open foundation year {state.open_year} != year {year} of the last week")
    amount, active, days, factor = admin_cost_for_year(params, year, inception, week)
    return AdminCost(year, active, days, factor, amount, week, "terminal")


@dataclass(frozen=True)
class FoundationTerminalResult:
    """Q-032 (foundation, tax_event=terminal): liquidation -> reserve consolidation ->
    final-year admin cost -> distributed_amount -> distribution tax -> after-tax wealth."""
    last_week: dt.date
    final_tax_year: int
    pre_terminal_ledger: Ledger
    pre_terminal_nav: float
    liquidation_trades: tuple
    terminal_realizations: tuple
    terminal_transaction_costs: float
    terminal_slippage: float
    terminal_trading_costs: float
    nav_after_liquidation: float
    final_admin_cost: AdminCost
    distributed_amount: float               # cash after liquidation costs and final admin cost
    distribution_tax_base_mode: str
    distribution_tax_base: float
    distribution_rate: float
    distribution_tax: float
    terminal_capital_gains_tax: float       # always 0 (no CG for foundations)
    terminal_solidarity_tax: float          # always 0
    terminal_foundation_tax: float          # = distribution tax
    terminal_tax_total: float
    terminal_transfers: tuple
    terminal_payments: tuple
    terminal_tax_events: tuple
    after_tax_terminal_wealth: float
    final_cash_ledger: Ledger
    tax_state_before_terminal: FoundationState
    final_tax_state: FoundationState

    def breakout(self) -> dict:
        return {
            "last_week": self.last_week.isoformat(), "final_tax_year": self.final_tax_year,
            "pre_terminal_nav": self.pre_terminal_nav,
            "terminal_transaction_costs": self.terminal_transaction_costs,
            "terminal_slippage": self.terminal_slippage,
            "terminal_liquidation_costs": self.terminal_trading_costs,
            "nav_after_liquidation": self.nav_after_liquidation,
            "final_admin_cost": self.final_admin_cost.amount,
            "final_admin_active_days": self.final_admin_cost.active_days,
            "distributed_amount": self.distributed_amount,
            "distribution_tax_base_mode": self.distribution_tax_base_mode,
            "distribution_tax_base": self.distribution_tax_base,
            "distribution_rate": self.distribution_rate,
            "terminal_capital_gains_tax": 0.0, "terminal_solidarity_tax": 0.0,
            "terminal_foundation_tax": self.terminal_foundation_tax,
            "terminal_tax_total": self.terminal_tax_total,
            "after_tax_terminal_wealth": self.after_tax_terminal_wealth,
            "final_cash_ledger": self.final_cash_ledger.components(),
            "liquidation_trades": len(self.liquidation_trades),
        }


def distribution_base(mode: str, distributed_amount: float, initial_capital: float) -> float:
    """FND-006, Q-032: distributed_amount, or gain_only = max(0, distributed - initial capital
    before the setup cost)."""
    if mode == "gain_only":
        return max(0.0, distributed_amount - initial_capital)
    return distributed_amount


def settle_foundation_terminal(snapshot: PortfolioSnapshot, params: FoundationParams,
                               state: FoundationState, initial_capital: float,
                               inception: dt.date) -> FoundationTerminalResult:
    """FND-003..007, FND-011, FND-012, Q-032: terminal settlement of a foundation on copies of
    the final snapshot and state; never a weekly record."""
    week, year = snapshot.week_key, tax_year(snapshot.week_key)
    before = state.copy()
    final = state.copy()
    pf = terminal_liquidation(snapshot, TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION)
    after_liq = pf.ledger
    trades, reals = tuple(pf.trades), tuple(pf.realizations)
    for r in reals:                         # audited; internal trading tax rate 0 (FND-002)
        final.realizations.setdefault(tax_year(r.week_key), []).append(
            (r.asset, r.realized_gain, r.week_key))
    n_events = len(final.tax_events)
    _consolidate_reserves(pf, week)
    cost = _final_admin_cost(params, final, inception, week)          # FND-011: no fake week
    if cost.amount > pf.ledger.rf_base * (1 + 1e-12) + 1e-12:
        raise InsolvencyError(week, cost.amount, pf.ledger.rf_base)
    final.tax_events.append(admin_cost_event(params, cost, "terminal"))
    if cost.amount > 0:
        pf.pay(week, cost.amount, ADMIN_COST, RF_BASE, "terminal_settlement", step=None, phase=PHASE)
    cost = dataclasses.replace(cost, paid_week=week)
    final.admin_cost_by_year[year] = cost
    final.closed_admin_years.append(year)
    final.admin_cost_paid += cost.amount
    final.open_year = None
    distributed = pf.ledger.rf_base
    base = distribution_base(params.distribution_tax_base, distributed, initial_capital)
    tax = base * params.distribution_rate
    final.tax_events.append(TaxEvent(
        week, DISTRIBUTION_TAX, "tax", "terminal", year, "portfolio", "", distributed, base,
        params.distribution_rate, tax, None,
        notes=f"distributed_amount={distributed!r} after liquidation costs and the final admin "
              f"cost; base={params.distribution_tax_base}"
              + (f" (distributed - initial capital {initial_capital!r})"
                 if params.distribution_tax_base == "gain_only" else "")
              + f"; {params.profile}", phase=PHASE))
    if tax > 0:
        pf.pay(week, tax, DISTRIBUTION_TAX, RF_BASE, "terminal_settlement", step=None, phase=PHASE)
    final.distribution_tax_paid += tax
    pf.ledger.check("foundation terminal settlement: ")
    return FoundationTerminalResult(
        last_week=week, final_tax_year=year, pre_terminal_ledger=snapshot.ledger,
        pre_terminal_nav=snapshot.ledger.nav, liquidation_trades=trades, terminal_realizations=reals,
        terminal_transaction_costs=math.fsum(t.transaction_cost for t in trades),
        terminal_slippage=math.fsum(t.slippage for t in trades),
        terminal_trading_costs=math.fsum([t.transaction_cost + t.slippage for t in trades]),
        nav_after_liquidation=after_liq.nav, final_admin_cost=cost, distributed_amount=distributed,
        distribution_tax_base_mode=params.distribution_tax_base, distribution_tax_base=base,
        distribution_rate=params.distribution_rate, distribution_tax=tax,
        terminal_capital_gains_tax=0.0, terminal_solidarity_tax=0.0, terminal_foundation_tax=tax,
        terminal_tax_total=tax, terminal_transfers=tuple(pf.transfers),
        terminal_payments=tuple(pf.payments), terminal_tax_events=tuple(final.tax_events[n_events:]),
        after_tax_terminal_wealth=pf.ledger.nav, final_cash_ledger=pf.ledger,
        tax_state_before_terminal=before, final_tax_state=final)


@dataclass(frozen=True)
class ShadowCostSettlement:
    """Q-015 foundation pre-tax: the final-year admin cost is a cost, not a tax, so the pre-tax
    wealth is the shadow's final NAV after paying it (TAX-006 waterfall, no full liquidation,
    no distribution tax)."""
    last_week: dt.date
    pre_cost_nav: float
    admin_cost: AdminCost
    trades: tuple
    payments: tuple
    final_ledger: Ledger
    final_wealth: float
    final_state: FoundationState


def settle_foundation_shadow_costs(snapshot: PortfolioSnapshot, params: FoundationParams,
                                   state: FoundationState, inception: dt.date,
                                   targets: dict, effective_states: dict) -> ShadowCostSettlement:
    week = snapshot.week_key
    final = state.copy()
    pf = WorkingPortfolio.from_snapshot(snapshot)
    cost = _final_admin_cost(params, final, inception, week)
    ctx = WeekContext(week, None, -1, dict(targets), {}, dict(effective_states), dict(effective_states))
    if cost.amount > 0:                     # rf_base -> reserves pro rata -> assets pro rata
        sell_to_pay(pf, ctx, AmountsDue(cost.amount, ((ADMIN_COST, cost.amount),)),
                    step=None, phase=PHASE)
    cost = dataclasses.replace(cost, paid_week=week)
    final.admin_cost_by_year[cost.year] = cost
    final.tax_events.append(admin_cost_event(params, cost, "terminal"))
    final.admin_cost_paid += cost.amount
    final.open_year = None
    pf.ledger.check("foundation shadow cost settlement: ")
    return ShadowCostSettlement(week, snapshot.ledger.nav, cost, tuple(pf.trades),
                                tuple(pf.payments), pf.ledger, pf.ledger.nav, final)
