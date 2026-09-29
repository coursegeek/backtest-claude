"""Central weekly portfolio engine (PORT-005/006/011/012, RISK-001..007, SIG-018/019, Q-017).

One engine serves every command. Its weekly transition follows PORT-011:

  INIT   step 0  capital hook (foundation setup cost later), signal reconstruction on the
                 whole pre-start history (no trades/taxes, SIG-019), initial sleeves from the
                 effective signal state (SIG-018), initial lots; no trade, no cost (Q-017)
  WEEK   step 1  execute previously scheduled signal trades
         step 2  amounts_due hook                        (annual taxes / admin cost later)
         step 3  rebalance-or-fund hook                  (rebalancing / sell_to_pay later)
         step 4  apply weekly returns to post-trade values (drifting weights, PORT-005/012)
         step 5  immediate-tax hook                      (dividend / RF taxes later)
         step 6  evaluate end-of-week signals, schedule future executions; end-of-week hook

Hooks receive the ``WorkingPortfolio`` and may only change it through its trade/payment
primitives, so taxes, rebalancing and sell_to_pay extend this loop instead of duplicating it.
The ledger invariants are checked after every step.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from .allocation import initial_sleeves
from .availability import evaluation_time
from .calendar import WEEK
from .cost_basis import CostBasisBook
from .costs import CostModel
from .errors import NotImplementedCommand
from .ledger import Ledger
from .models import RISKY_ASSETS, State, Trade, TradeReason, canonical_assets
from .rf import RF_BASE, grow, reserve_name
from .signal_analysis import reconstruct_tracker

TOL = 1e-12


# ============================================================================ data
@dataclass(frozen=True)
class WeekMarket:
    """Returns applied in step 4 of one run week. ``dividend_yield`` is reserved for the
    dividend/cost-basis decomposition (Q-016) and unused in this build."""
    week_key: dt.date
    asset_returns: dict              # active risky asset -> total weekly return
    rf_return: float
    dividend_yield: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EngineInputs:
    weeks: tuple                     # executed run calendar (ascending Friday keys)
    market: dict                     # week -> WeekMarket
    signal_series: dict              # active asset -> PriceSeries (signal price)
    params: dict                     # active asset -> SignalParams
    targets: dict                    # stocks, gold, btc, rf (sum 1)
    initial_capital: float
    costs: CostModel = CostModel()
    cost_basis_method: str = "FIFO"
    trace: bool = False              # keep the ledger after every pipeline step


@dataclass(frozen=True)
class AmountsDue:
    total: float = 0.0
    items: tuple = ()                # (event_type, amount) for the future tax module


@dataclass(frozen=True)
class WeekRecord:
    week_key: dt.date
    ledger_start: Ledger
    ledger_after_trades: Ledger
    ledger_end: Ledger
    nav_start: float
    nav_end: float
    portfolio_return: float
    market: WeekMarket
    trades: tuple
    amounts_due: AmountsDue
    effective_states: dict
    confirmed_states: dict
    step_ledgers: tuple = ()         # ((step, Ledger), ...) when inputs.trace


@dataclass(frozen=True)
class EngineResult:
    initial_ledger: Ledger
    weeks: tuple
    trades: tuple
    signal_records: tuple
    realizations: tuple
    pre_start: dict
    final_ledger: Ledger
    final_lots: dict
    issues: tuple


# ============================================================================ portfolio
class WorkingPortfolio:
    """Mutable state of one run. Every change goes through a primitive that keeps the
    ledger, the cost basis book and the trade journal consistent."""

    def __init__(self, ledger: Ledger, costs: CostModel, method: str, inception: dt.date):
        self.ledger = ledger
        self.costs = costs
        self.book = CostBasisBook(method)
        self.unit_price = {a: 1.0 for a in RISKY_ASSETS}
        self.trades: list = []
        self.realizations: list = []
        for a in RISKY_ASSETS:
            v = ledger.asset(a)
            if v > 0:
                self.book.open_lot(a, inception, v / self.unit_price[a], v, "initial")

    # ---------------------------------------------------------------- primitives
    def sell(self, week, asset, gross, reason, cash_component, confirm_week=None, step=1) -> Optional[Trade]:
        """Sell ``gross`` of ``asset``; net proceeds credited to ``cash_component`` (Q-017)."""
        before = self.ledger.asset(asset)
        if gross <= 0:
            return None
        if gross > before * (1 + TOL):
            raise ValueError(f"cannot sell {gross!r} of {asset}, holding {before!r}")
        gross = min(gross, before)
        cost, slip, net = self.costs.sale(gross)
        res_before = getattr(self.ledger, cash_component)
        after = 0.0 if gross == before else before - gross
        real = self.book.sell_fraction(asset, gross / before, week, net)
        self.realizations.append(real)
        self.ledger = self.ledger.replace(**{asset: after, cash_component: res_before + net})
        trade = Trade(week, asset, "sell", reason, gross, cost, slip, net, before, after,
                      res_before, res_before + net, cash_component, real.units_sold,
                      real.cost_basis, real.realized_gain, confirm_week, step)
        self.trades.append(trade)
        return trade

    def buy_with_cash(self, week, asset, cash, reason, cash_component, confirm_week=None,
                      step=1) -> Optional[Trade]:
        """Spend ``cash`` of ``cash_component`` (all of it: costs included) on ``asset``."""
        available = getattr(self.ledger, cash_component)
        if cash <= 0:
            return None
        if cash > available * (1 + TOL):
            raise ValueError(f"{cash_component} holds {available!r}, cannot spend {cash!r}")
        spend = min(cash, available)
        gross, cost, slip = self.costs.purchase_from_cash(spend)
        before = self.ledger.asset(asset)
        res_after = 0.0 if spend == available else available - spend   # never negative
        units = gross / self.unit_price[asset]
        self.book.open_lot(asset, week, units, spend, "buy")    # basis = cash spent incl. costs
        self.ledger = self.ledger.replace(**{asset: before + gross, cash_component: res_after})
        trade = Trade(week, asset, "buy", reason, gross, cost, slip, -spend, before,
                      before + gross, available, res_after, cash_component, units, spend, None,
                      confirm_week, step)
        self.trades.append(trade)
        return trade

    def apply_returns(self, market: WeekMarket) -> None:
        """Step 4: returns applied to the actual post-trade values; RF base and every RF
        reserve earn the same gross RF return."""
        changes = {}
        for a in RISKY_ASSETS:
            r = market.asset_returns.get(a)
            if r is None:
                if self.ledger.asset(a) != 0:
                    raise ValueError(f"missing {a} return for {market.week_key}")
                continue
            changes[a] = self.ledger.asset(a) * (1.0 + r)
            self.unit_price[a] *= 1.0 + r
        for c in (RF_BASE,) + tuple(reserve_name(a) for a in RISKY_ASSETS):
            changes[c] = grow(getattr(self.ledger, c), market.rf_return)
        self.ledger = self.ledger.replace(**changes)

    def lots_snapshot(self) -> dict:
        return {a: self.book.lots(a) for a in RISKY_ASSETS}


# ============================================================================ hooks
class PipelineHooks:
    """Extension points of the weekly pipeline. The defaults implement tax.profile=none
    with portfolio.rebalance=signal-only; taxes, rebalancing and sell_to_pay subclass this."""

    def investable_capital(self, capital: float) -> float:            # step 0
        return capital

    def amounts_due(self, week, portfolio) -> AmountsDue:              # step 2
        return AmountsDue()

    def rebalance_or_fund(self, week, portfolio, due: AmountsDue, targets) -> None:   # step 3
        if due.total > 0:
            raise NotImplementedCommand("amounts due require sell_to_pay/rebalancing, "
                                        "which are not implemented in this build")

    def immediate_taxes(self, week, portfolio, ledger_before_returns, market) -> None:  # step 5
        return None

    def end_of_week(self, week, portfolio) -> None:                    # step 6 (band triggers later)
        return None


# ============================================================================ engine
class Engine:
    def __init__(self, inputs: EngineInputs, hooks: Optional[PipelineHooks] = None):
        if not inputs.weeks:
            raise ValueError("empty run calendar")
        self.inputs = inputs
        self.hooks = hooks or PipelineHooks()
        self.assets = canonical_assets(inputs.params)
        self.first_week = inputs.weeks[0]
        self.inception = self.first_week - WEEK

    def _reconstruct(self):
        trackers, pre, issues = {}, {}, []
        for a in self.assets:
            tracker, state = reconstruct_tracker(self.inputs.signal_series[a], self.inputs.params[a],
                                                 self.first_week)
            trackers[a], pre[a] = tracker, state
            issues += state.issues
        return trackers, pre, issues

    def run(self) -> EngineResult:
        inp = self.inputs
        trackers, pre, issues = self._reconstruct()
        capital = self.hooks.investable_capital(inp.initial_capital)
        sleeves = initial_sleeves(capital, inp.targets,
                                  {a: pre[a].effective_state for a in self.assets}, inp.params)
        initial = Ledger.from_sleeves(sleeves)
        initial.check("initial allocation: ")
        pf = WorkingPortfolio(initial, inp.costs, inp.cost_basis_method, self.inception)
        points = {a: list(inp.signal_series[a].points) for a in self.assets}
        cursor = {a: next((i for i, p in enumerate(points[a]) if p.week_key >= self.first_week),
                          len(points[a])) for a in self.assets}
        weeks, signal_records = [], []
        for week in inp.weeks:
            start = pf.ledger
            n_trades = len(pf.trades)
            trace = []

            def snap(step):
                pf.ledger.check(f"{week} step {step}: ")
                if inp.trace:
                    trace.append((step, pf.ledger))

            # 1. previously scheduled signal trades
            executed = {}
            for a in self.assets:
                executed[a] = trackers[a].due(week)
                for ex, noop in executed[a]:
                    if not noop:
                        self._execute_signal(pf, week, a, ex)
            snap(1)
            after_trades = pf.ledger
            # 2. amounts due
            due = self.hooks.amounts_due(week, pf)
            snap(2)
            # 3. strategic rebalance / sell_to_pay
            self.hooks.rebalance_or_fund(week, pf, due, inp.targets)
            snap(3)
            # 4. weekly returns on actual post-trade values
            before_returns = pf.ledger
            market = inp.market[week]
            pf.apply_returns(market)
            snap(4)
            # 5. immediate taxes
            self.hooks.immediate_taxes(week, pf, before_returns, market)
            snap(5)
            # 6. end-of-week signals (all observations up to this week, available by Sunday)
            for a in self.assets:
                pts, i = points[a], cursor[a]
                while i < len(pts) and pts[i].week_key <= week:
                    if pts[i].available_at > evaluation_time(week):
                        break
                    signal_records.append(trackers[a].observe(
                        pts[i], executed[a] if pts[i].week_key == week else ()))
                    i += 1
                cursor[a] = i
            self.hooks.end_of_week(week, pf)
            snap(6)
            end = pf.ledger
            weeks.append(WeekRecord(
                week, start, after_trades, end, start.nav, end.nav, end.nav / start.nav - 1.0,
                market, tuple(pf.trades[n_trades:]), due,
                {a: trackers[a].effective_state for a in self.assets},
                {a: trackers[a].machine.state for a in self.assets}, tuple(trace)))
        return EngineResult(initial, tuple(weeks), tuple(pf.trades), tuple(signal_records),
                            tuple(pf.realizations), pre, pf.ledger, pf.lots_snapshot(), tuple(issues))

    def _execute_signal(self, pf: WorkingPortfolio, week, asset, ex) -> None:
        params = self.inputs.params[asset]
        reserve = reserve_name(asset)
        if ex.target_state == State.RISK_OFF:
            value = pf.ledger.asset(asset)
            if params.risk_off_action == "target_fraction_of_sleeve":
                target = (1.0 - params.sell_fraction) * pf.ledger.sleeve(asset)
                gross = max(0.0, value - target)
            else:                                   # RISK-001: fraction of the current value
                gross = value * params.sell_fraction
            pf.sell(week, asset, gross, TradeReason.SIGNAL_EXIT, reserve, ex.confirm_week)
        else:                                       # RISK-005: only the dedicated reserve
            pf.buy_with_cash(week, asset, pf.ledger.reserve(asset), TradeReason.SIGNAL_REENTRY,
                             reserve, ex.confirm_week)


def run_engine(inputs: EngineInputs, hooks: Optional[PipelineHooks] = None) -> EngineResult:
    return Engine(inputs, hooks).run()
