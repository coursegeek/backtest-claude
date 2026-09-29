"""Central weekly portfolio engine (PORT-005/006/011/012, RISK-001..007, SIG-018/019, Q-017).

One engine serves every command. Its weekly transition follows PORT-011:

  INIT   step 0  capital hook (foundation setup cost later), signal reconstruction on the
                 whole pre-start history (no trades/taxes, SIG-019), initial sleeves from the
                 effective signal state (SIG-018), initial lots; no trade, no cost (Q-017)
  WEEK   step 1  execute previously scheduled signal trades          -> ledger_after_signal
         step 2  amounts_due hook                        (annual taxes / admin cost later)
         step 3  rebalance-or-fund hook (strategic rebalance / sell_to_pay, payments)
                                                                     -> ledger_before_returns
         step 4  apply weekly returns to ledger_before_returns (PORT-005/012)
         step 5  immediate-tax hook                      (dividend / RF taxes later)
         step 6  evaluate end-of-week signals, schedule future executions; end-of-week hook
                                                                     -> ledger_end

Hooks receive a ``WeekContext`` and the ``WorkingPortfolio`` and may change the portfolio only
through its primitives (sell, buy, buy_with_cash, pay, transfer), so taxes, rebalancing and
sell_to_pay extend this loop instead of duplicating it. Ledger invariants are checked after
every step.

Time axis: ``EngineInputs.weeks`` is the retained run calendar. Weeks removed by
missing.return_policy=drop (or common calendar gaps) are not on the portfolio time axis: their
signal observations are not fed to the in-run signal state machine (they are reported as
skipped), and an execution whose nominal week was removed runs in the next retained week
while keeping its nominal week in the audit trail.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

from .allocation import initial_sleeves
from .availability import evaluation_time
from .calendar import WEEK
from .cost_basis import CostBasisBook
from .costs import CostModel
from .errors import NotImplementedCommand
from .ledger import COMPONENTS, Ledger
from .models import (RISKY_ASSETS, Payment, RfTransfer, State, Trade, TradeReason,
                     canonical_assets)
from .rf import RF_BASE, grow, reserve_name
from .signal_analysis import reconstruct_tracker

TOL = 1e-12


# ============================================================================ data
@dataclass(frozen=True)
class WeekMarket:
    """Returns applied in step 4 of one run week."""
    week_key: dt.date
    asset_returns: dict              # active risky asset -> total weekly return
    rf_return: float
    dividend_yield: dict = field(default_factory=dict)   # reserved for Q-016

    def unit_price_return(self, asset: str) -> float:
        """Return that moves the cost-basis unit price of ``asset``.

        TODO(Q-016): with dividend taxation the unit price must move by the price component
        only (total - gross dividend yield), while the net dividend after tax opens new lots.
        Until that is implemented no dividend yield may be supplied, so the total return is
        the unit-price return (valid for tax.profile=none). New code must call this method
        instead of reading asset_returns directly for cost-basis purposes."""
        if self.dividend_yield:
            raise NotImplementedCommand("dividend decomposition of the unit price (Q-016) is "
                                        "not implemented")
        return self.asset_returns[asset]


@dataclass(frozen=True)
class EngineInputs:
    weeks: tuple                     # retained run calendar (ascending Friday keys)
    market: dict                     # week -> WeekMarket
    signal_series: dict              # active asset -> PriceSeries (signal price)
    params: dict                     # active asset -> SignalParams
    targets: dict                    # stocks, gold, btc, rf (sum 1)
    initial_capital: float
    costs: CostModel = CostModel()
    cost_basis_method: str = "FIFO"
    trace: bool = False              # keep the ledger after every pipeline step
    run_start: Optional[dt.date] = None   # first calendar week of the run (default weeks[0])


@dataclass(frozen=True)
class AmountsDue:
    """Cash that must actually be paid in the week (Q-046); items are (event_type, amount)."""
    total: float = 0.0
    items: tuple = ()

    def normalized_items(self) -> tuple:
        if self.total < 0:
            raise ValueError("amounts due must be >= 0")
        if not self.items:
            return (("amount_due", self.total),) if self.total > 0 else ()
        if abs(math.fsum(a for _, a in self.items) - self.total) > 1e-9 * max(1.0, self.total):
            raise ValueError("amounts due items do not sum to total")
        return tuple(self.items)


@dataclass(frozen=True)
class WeekContext:
    """Information available to hooks in a week (no future data)."""
    week: dt.date
    prev_week: Optional[dt.date]     # previous retained run week (None in the first week)
    index: int
    targets: dict
    params: dict
    effective_states: dict           # after step 1 executions
    confirmed_states: dict


@dataclass(frozen=True)
class WeekRecord:
    week_key: dt.date
    ledger_start: Ledger
    ledger_after_signal: Ledger      # after step 1
    ledger_before_returns: Ledger    # after steps 2-3 (all start-of-week trades and payments)
    ledger_end: Ledger
    portfolio_return: float
    market: WeekMarket
    trades: tuple
    payments: tuple
    transfers: tuple
    rebalance: Optional[object]      # RebalanceEvent or None
    amounts_due: AmountsDue
    effective_states: dict
    confirmed_states: dict
    step_ledgers: tuple = ()         # ((step, Ledger), ...) when inputs.trace

    @property
    def nav_start(self) -> float:
        return self.ledger_start.nav

    @property
    def nav_after_signal(self) -> float:
        return self.ledger_after_signal.nav

    @property
    def nav_before_returns(self) -> float:
        return self.ledger_before_returns.nav

    @property
    def nav_end(self) -> float:
        return self.ledger_end.nav


@dataclass(frozen=True)
class EngineResult:
    initial_ledger: Ledger
    weeks: tuple
    trades: tuple
    payments: tuple
    transfers: tuple
    rebalance_events: tuple
    signal_records: tuple
    skipped_signal_observations: tuple   # (asset, week_key) in weeks off the time axis
    realizations: tuple
    pre_start: dict
    final_ledger: Ledger
    final_lots: dict
    issues: tuple


# ============================================================================ portfolio
class WorkingPortfolio:
    """Mutable state of one run. Every change goes through a primitive that keeps the
    ledger, the cost basis book and the audit journals consistent."""

    def __init__(self, ledger: Ledger, costs: CostModel, method: str, inception: dt.date):
        self.ledger = ledger
        self.costs = costs
        self.book = CostBasisBook(method)
        self.unit_price = {a: 1.0 for a in RISKY_ASSETS}
        self.trades: list = []
        self.payments: list = []
        self.transfers: list = []
        self.rebalance_events: list = []
        self.realizations: list = []
        for a in RISKY_ASSETS:
            v = ledger.asset(a)
            if v > 0:
                self.book.open_lot(a, inception, v / self.unit_price[a], v, "initial")

    def _component(self, name: str) -> float:
        if name not in COMPONENTS:
            raise KeyError(name)
        return getattr(self.ledger, name)

    # ---------------------------------------------------------------- trades
    def sell(self, week, asset, gross, reason, cash_component, confirm_week=None, step=1,
             nominal_week=None) -> Optional[Trade]:
        """Sell ``gross`` of ``asset``; net proceeds credited to ``cash_component`` (Q-017)."""
        before = self.ledger.asset(asset)
        if gross <= 0:
            return None
        if gross > before * (1 + TOL):
            raise ValueError(f"cannot sell {gross!r} of {asset}, holding {before!r}")
        gross = min(gross, before)
        cost, slip, net = self.costs.sale(gross)
        cash_before = self._component(cash_component)
        after = 0.0 if gross == before else before - gross
        real = self.book.sell_fraction(asset, gross / before, week, net)
        self.realizations.append(real)
        self.ledger = self.ledger.replace(**{asset: after, cash_component: cash_before + net})
        trade = Trade(week, asset, "sell", reason, gross, cost, slip, net, before, after,
                      cash_before, cash_before + net, cash_component, real.units_sold,
                      real.cost_basis, real.realized_gain, confirm_week, step, nominal_week)
        self.trades.append(trade)
        return trade

    def buy(self, week, asset, traded_value, reason, cash_component, step=3) -> Optional[Trade]:
        """Buy ``traded_value`` of ``asset``; spends traded_value*(1+c) of ``cash_component``."""
        if traded_value <= 0:
            return None
        spend = traded_value * (1.0 + self.costs.tc_rate + self.costs.slip_rate)
        return self._purchase(week, asset, spend, traded_value, reason, cash_component, None,
                              step, None)

    def buy_with_cash(self, week, asset, cash, reason, cash_component, confirm_week=None,
                      step=1, nominal_week=None) -> Optional[Trade]:
        """Spend ``cash`` of ``cash_component`` (costs included) on ``asset``."""
        if cash <= 0:
            return None
        gross, _, _ = self.costs.purchase_from_cash(cash)
        return self._purchase(week, asset, cash, gross, reason, cash_component, confirm_week,
                              step, nominal_week)

    def _purchase(self, week, asset, spend, gross, reason, cash_component, confirm_week, step,
                  nominal_week):
        available = self._component(cash_component)
        if spend > available * (1 + TOL) + TOL:
            raise ValueError(f"{cash_component} holds {available!r}, cannot spend {spend!r}")
        spend = min(spend, available)
        cost, slip = gross * self.costs.tc_rate, gross * self.costs.slip_rate
        before = self.ledger.asset(asset)
        cash_after = 0.0 if spend == available else available - spend   # never negative
        units = gross / self.unit_price[asset]
        self.book.open_lot(asset, week, units, spend, "buy")    # basis = cash spent incl. costs
        self.ledger = self.ledger.replace(**{asset: before + gross, cash_component: cash_after})
        trade = Trade(week, asset, "buy", reason, gross, cost, slip, -spend, before,
                      before + gross, available, cash_after, cash_component, units, spend, None,
                      confirm_week, step, nominal_week)
        self.trades.append(trade)
        return trade

    # ---------------------------------------------------------------- cash
    def pay(self, week, amount, event_type, source, context, step=3) -> Optional[Payment]:
        """NAV outflow from an RF component (never negative)."""
        if amount <= 0:
            return None
        available = self._component(source)
        if source not in (RF_BASE,) + tuple(reserve_name(a) for a in RISKY_ASSETS):
            raise ValueError(f"payments are made from RF components only, not {source}")
        if amount > available * (1 + TOL) + TOL:
            raise ValueError(f"{source} holds {available!r}, cannot pay {amount!r}")
        amount = min(amount, available)
        self.ledger = self.ledger.replace(**{source: 0.0 if amount == available else available - amount})
        p = Payment(week, event_type, amount, step, source, context)
        self.payments.append(p)
        return p

    def transfer(self, week, source, destination, amount, reason, step=3) -> Optional[RfTransfer]:
        """Cost-free book transfer between RF components (Q-017)."""
        rf = (RF_BASE,) + tuple(reserve_name(a) for a in RISKY_ASSETS)
        if source not in rf or destination not in rf:
            raise ValueError("transfers are allowed between RF components only")
        if amount <= 0:
            return None
        available = self._component(source)
        if amount > available * (1 + TOL) + TOL:
            raise ValueError(f"{source} holds {available!r}, cannot transfer {amount!r}")
        amount = min(amount, available)
        self.ledger = self.ledger.replace(**{
            source: 0.0 if amount == available else available - amount,
            destination: self._component(destination) + amount})
        t = RfTransfer(week, source, destination, amount, reason, step)
        self.transfers.append(t)
        return t

    # ---------------------------------------------------------------- returns
    def apply_returns(self, market: WeekMarket) -> None:
        """Step 4: returns applied to the actual values after all start-of-week transactions;
        RF base and every RF reserve earn the same gross RF return."""
        changes = {}
        for a in RISKY_ASSETS:
            r = market.asset_returns.get(a)
            if r is None:
                if self.ledger.asset(a) != 0:
                    raise ValueError(f"missing {a} return for {market.week_key}")
                continue
            changes[a] = self.ledger.asset(a) * (1.0 + r)
            self.unit_price[a] *= 1.0 + market.unit_price_return(a)
        for c in (RF_BASE,) + tuple(reserve_name(a) for a in RISKY_ASSETS):
            changes[c] = grow(getattr(self.ledger, c), market.rf_return)
        self.ledger = self.ledger.replace(**changes)

    def lots_snapshot(self) -> dict:
        return {a: self.book.lots(a) for a in RISKY_ASSETS}


# ============================================================================ hooks
class PipelineHooks:
    """Extension points of the weekly pipeline (steps 0, 2, 3, 5, 6). The base class is the
    neutral policy: no amounts due, no strategic rebalance, no immediate taxes. Strategic
    rebalancing and sell_to_pay live in rebalancing.StrategicHooks; taxes will extend it."""

    def investable_capital(self, capital: float) -> float:              # step 0
        return capital

    def amounts_due(self, ctx: WeekContext, portfolio) -> AmountsDue:    # step 2
        return AmountsDue()

    def rebalance_or_fund(self, ctx: WeekContext, portfolio, due: AmountsDue) -> None:   # step 3
        if due.total > 0:
            raise NotImplementedCommand("amounts due need a step-3 funding policy "
                                        "(rebalancing.StrategicHooks)")

    def immediate_taxes(self, ctx: WeekContext, portfolio, ledger_before_returns, market) -> None:
        return None                                                      # step 5

    def end_of_week(self, ctx: WeekContext, portfolio) -> None:          # step 6
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
        self.run_start = inputs.run_start or self.first_week
        if self.run_start > self.first_week:
            raise ValueError("run_start after the first retained week")
        self.inception = self.first_week - WEEK

    def _reconstruct(self):
        trackers, pre, issues = {}, {}, []
        for a in self.assets:
            tracker, state = reconstruct_tracker(self.inputs.signal_series[a],
                                                 self.inputs.params[a], self.run_start)
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
        cursor = {a: next((i for i, p in enumerate(points[a]) if p.week_key >= self.run_start),
                          len(points[a])) for a in self.assets}
        weeks, signal_records, skipped = [], [], []
        prev_week = None
        for idx, week in enumerate(inp.weeks):
            start = pf.ledger
            marks = (len(pf.trades), len(pf.payments), len(pf.transfers), len(pf.rebalance_events))
            trace = []

            def snap(step):
                pf.ledger.check(f"{week} step {step}: ")
                if inp.trace:
                    trace.append((step, pf.ledger))

            # 1. previously scheduled signal trades (executions nominally due in removed
            #    weeks run now, in the first retained week, keeping their nominal week)
            executed = {}
            for a in self.assets:
                executed[a] = trackers[a].due(week)
                for ex, noop in executed[a]:
                    if not noop:
                        self._execute_signal(pf, week, a, ex)
            snap(1)
            after_signal = pf.ledger
            ctx = WeekContext(week, prev_week, idx, inp.targets, inp.params,
                              {a: trackers[a].effective_state for a in self.assets},
                              {a: trackers[a].machine.state for a in self.assets})
            # 2. amounts due
            due = self.hooks.amounts_due(ctx, pf)
            due_items = due.normalized_items()
            snap(2)
            # 3. strategic rebalance or sell_to_pay; amounts due must be paid in this step
            self.hooks.rebalance_or_fund(ctx, pf, due)
            paid = math.fsum(p.amount for p in pf.payments[marks[1]:])
            if due_items and abs(paid - due.total) > 1e-9 * max(1.0, due.total):
                raise ValueError(f"{week}: amounts due {due.total!r} but step 3 paid {paid!r}")
            snap(3)
            before_returns = pf.ledger
            # 4. weekly returns on the holdings after all start-of-week transactions
            market = inp.market[week]
            pf.apply_returns(market)
            snap(4)
            # 5. immediate taxes
            self.hooks.immediate_taxes(ctx, pf, before_returns, market)
            snap(5)
            # 6. end-of-week signals: only the observation of this retained week
            for a in self.assets:
                pts, i = points[a], cursor[a]
                while i < len(pts) and pts[i].week_key < week:
                    skipped.append((a, pts[i].week_key))     # week off the portfolio time axis
                    i += 1
                if i < len(pts) and pts[i].week_key == week:
                    if pts[i].available_at <= evaluation_time(week):
                        signal_records.append(trackers[a].observe(pts[i], executed[a]))
                        i += 1
                cursor[a] = i
            self.hooks.end_of_week(ctx, pf)
            snap(6)
            end = pf.ledger
            rebal = pf.rebalance_events[marks[3]:]
            weeks.append(WeekRecord(
                week, start, after_signal, before_returns, end, end.nav / start.nav - 1.0,
                market, tuple(pf.trades[marks[0]:]), tuple(pf.payments[marks[1]:]),
                tuple(pf.transfers[marks[2]:]), rebal[-1] if rebal else None, due,
                {a: trackers[a].effective_state for a in self.assets},
                {a: trackers[a].machine.state for a in self.assets}, tuple(trace)))
            prev_week = week
        return EngineResult(initial, tuple(weeks), tuple(pf.trades), tuple(pf.payments),
                            tuple(pf.transfers), tuple(pf.rebalance_events),
                            tuple(signal_records), tuple(skipped), tuple(pf.realizations), pre,
                            pf.ledger, pf.lots_snapshot(), tuple(issues))

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
            pf.sell(week, asset, gross, TradeReason.SIGNAL_EXIT, reserve, ex.confirm_week,
                    nominal_week=ex.execution_week)
        else:                                       # RISK-005: only the dedicated reserve
            pf.buy_with_cash(week, asset, pf.ledger.reserve(asset), TradeReason.SIGNAL_REENTRY,
                             reserve, ex.confirm_week, nominal_week=ex.execution_week)


def run_engine(inputs: EngineInputs, hooks: Optional[PipelineHooks] = None) -> EngineResult:
    return Engine(inputs, hooks).run()
