"""Strategic rebalancing and sell_to_pay (REB-001, REB-006..009, TAX-006, TAX-007, PORT-011
step 3, Q-017, Q-039, Q-046).

Separated into
  * trigger detection  - calendar_trigger(), band_deviation()
  * planning           - plan_rebalance(): cost-aware, exact, key-order independent
  * execution          - execute_rebalance(), sell_to_pay()
and wired into the central engine through ``StrategicHooks`` (steps 3 and 6); without a
trigger, positive amounts due are funded by ``sell_to_pay`` (src/sell_to_pay.py, TAX-006).

Rebalance planning (TAX-007, TEST-054). With NAV0 = NAV after signal trades, D = amounts due,
c = transaction cost rate + slippage rate, strategic targets w_a (a in stocks, gold, btc) and
w_rf, and the asset share s_a of each sleeve (1 for RISK_ON, current asset/sleeve for RISK_OFF,
REB-009), the final NAV F solves

    F + c * sum_a |s_a w_a F - A_a| = NAV0 - D

(asset trades cost c per unit traded; RF transfers are free). The left side is strictly
increasing and piecewise linear in F, so F is found exactly on the segment between sorted
breakpoints A_a/(s_a w_a). Targets are then asset_a = s_a w_a F, reserve_a = (1-s_a) w_a F,
rf_base = w_rf F. Sums use math.fsum, which is exactly rounded and therefore independent of
the order of the assets.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

from .calendar import WEEK, is_first_week_of_period
from .costs import CostModel
from .engine import AmountsDue, PipelineHooks, WeekContext
from .errors import ConfigError, InsolvencyError
from .models import RISKY_ASSETS, RebalanceEvent, State, TradeReason, canonical_assets
from .rf import RF_BASE, reserve_name
from .sell_to_pay import DueCursor, sell_to_pay

MODES = ("signal-only", "weekly", "monthly", "quarterly", "annually", "band")
ALIASES = {"yearly": "annually"}
CALENDAR_MODES = ("weekly", "monthly", "quarterly", "annually")
BAND_TOL = 1e-12
DUST_REL = 1e-12


def normalize_mode(mode: str) -> str:
    mode = ALIASES.get(mode, mode)
    if mode not in MODES:
        raise ConfigError(f"portfolio.rebalance: {mode!r} not in {MODES} (or 'yearly')")
    return mode


# ============================================================================ triggers
def calendar_trigger(mode: str, prev_week: Optional[dt.date], week: dt.date) -> bool:
    """REB-006/Q-039: first retained record whose Friday week_key is in a new period relative
    to the previous retained record; the first record (initial allocation) never triggers."""
    if mode not in CALENDAR_MODES or prev_week is None:
        return False
    return is_first_week_of_period(prev_week, week, mode)


def band_deviation(ledger, targets: dict) -> tuple:
    """REB-007: largest |actual sleeve weight - target| over all strategic sleeves
    (stocks, gold, btc sleeves incl. reserves, and rf_base). Returns (max_dev, per_sleeve)."""
    w = ledger.sleeve_weights()
    devs = {s: abs(w[s] - targets[s]) for s in sorted(w)}
    return max(devs.values()), devs


def band_breached(ledger, targets: dict, band_pp: float) -> tuple:
    """Deviation in percentage points: band_pp=1 means 0.01 absolute weight, not 1% relative."""
    m, devs = band_deviation(ledger, targets)
    return m >= band_pp / 100.0 - BAND_TOL, m


# ============================================================================ planning
@dataclass(frozen=True)
class RebalancePlan:
    nav_after_signal: float
    amounts_due: float
    cost_rate: float
    final_nav: float
    asset_share: dict            # a -> s_a
    target_asset: dict           # a -> s_a w_a F
    target_reserve: dict         # a -> (1-s_a) w_a F
    target_rf_base: float
    deltas: dict                 # a -> target_asset - current asset (+ buy, - sell)


def asset_shares(ledger, effective_states: dict, params: dict) -> dict:
    """REB-009: RISK_ON sleeves stay 100% in the asset; RISK_OFF sleeves keep their current
    asset/reserve split (or the initial risk-off split when the sleeve is empty)."""
    out = {}
    for a in RISKY_ASSETS:
        state = effective_states.get(a, State.RISK_ON)
        if state == State.RISK_ON:
            out[a] = 1.0
        else:
            total = ledger.sleeve(a)
            out[a] = ledger.asset(a) / total if total > 0 else 1.0 - params[a].sell_fraction
    return out


def solve_final_nav(assets: dict, k: dict, available: float, c: float) -> float:
    """Solve F + c*sum|k_a F - A_a| = available for F >= 0 (unique; exact on its segment)."""
    def g(f):
        return f + c * math.fsum(abs(k[a] * f - assets[a]) for a in assets) - available

    if c == 0.0:
        f = available
    else:
        if g(0.0) > 0:
            return -1.0                                   # insolvent: caller raises
        bps = sorted({assets[a] / k[a] for a in assets if k[a] > 0 and assets[a] > 0})
        lo = 0.0
        for bp in bps:
            if g(bp) >= 0:
                hi = bp
                break
            lo = bp
        else:
            hi = None
        probe = lo + 1.0 if hi is None else (lo + hi) / 2.0
        slope = 1.0 + c * math.fsum((k[a] if k[a] * probe > assets[a] else -k[a]) for a in assets)
        f = lo - g(lo) / slope
    return f


def plan_rebalance(ledger, targets: dict, due: float, costs: CostModel, shares: dict,
                   week=None) -> RebalancePlan:
    nav0 = ledger.nav
    c = costs.tc_rate + costs.slip_rate
    A = {a: ledger.asset(a) for a in RISKY_ASSETS}
    k = {a: shares[a] * targets[a] for a in RISKY_ASSETS}
    available = nav0 - due
    f = solve_final_nav(A, k, available, c)
    if f < 0:
        net_liquidation = nav0 - c * math.fsum(A.values())
        raise InsolvencyError(week, due, net_liquidation)
    t_asset = {a: k[a] * f for a in RISKY_ASSETS}
    t_res = {a: (targets[a] - k[a]) * f for a in RISKY_ASSETS}
    deltas = {a: t_asset[a] - A[a] for a in RISKY_ASSETS}
    return RebalancePlan(nav0, due, c, f, dict(shares), t_asset, t_res, targets["rf"] * f, deltas)


# ============================================================================ execution
def execute_rebalance(pf, ctx: WeekContext, due: AmountsDue, reason: TradeReason, mode: str,
                      source_week, nominal_week, max_dev=None) -> RebalanceEvent:
    """TAX-007 order: plan -> sells -> (reserve releases) -> pay amounts due -> buys
    (-> reserve top-ups). Trades settle against rf_base; RF moves are cost-free transfers."""
    week = ctx.week
    before = pf.ledger
    shares = asset_shares(before, ctx.effective_states, ctx.params)
    plan = plan_rebalance(before, ctx.targets, due.total, pf.costs, shares, week)
    dust = DUST_REL * max(1.0, plan.nav_after_signal)
    n_trades = len(pf.trades)
    assets = canonical_assets(RISKY_ASSETS)
    # 3b: sells and reserve releases
    for a in assets:
        if plan.deltas[a] < -dust:
            pf.sell(week, a, -plan.deltas[a], reason, RF_BASE, step=3)
    for a in assets:
        release = pf.ledger.reserve(a) - plan.target_reserve[a]
        if release > dust:
            pf.transfer(week, reserve_name(a), RF_BASE, release, f"{reason.value}_reserve_release")
    # 3c: amounts due from the proceeds
    DueCursor(pf, week, due).pay(RF_BASE, due.total, "strategic_rebalance")
    # 3d: buys and reserve top-ups
    for a in assets:
        if plan.deltas[a] > dust:
            pf.buy(week, a, plan.deltas[a], reason, RF_BASE, step=3)
    for a in assets:
        top_up = plan.target_reserve[a] - pf.ledger.reserve(a)
        if top_up > dust:
            pf.transfer(week, RF_BASE, reserve_name(a), min(top_up, pf.ledger.rf_base),
                        f"{reason.value}_reserve_top_up")
    trades = pf.trades[n_trades:]
    after = pf.ledger
    event = RebalanceEvent(
        week, mode, reason, source_week, nominal_week, plan.nav_after_signal, due.total,
        plan.nav_after_signal - due.total, plan.final_nav, after.nav,
        math.fsum(t.transaction_cost for t in trades), math.fsum(t.slippage for t in trades),
        before.sleeve_weights(), after.sleeve_weights(), max_dev)
    pf.rebalance_events.append(event)
    return event


class StrategicHooks(PipelineHooks):
    """Step 3: strategic rebalance (calendar or band trigger) or, without a trigger,
    sell_to_pay for positive amounts due. Step 6: band trigger detection."""

    def __init__(self, mode: str = "signal-only", band_pp: Optional[float] = None):
        self.mode = normalize_mode(mode)
        if self.mode == "band" and not (band_pp and band_pp > 0):
            raise ConfigError("band rebalancing requires portfolio.rebalance_band_pp > 0")
        self.band_pp = band_pp
        self.pending = None           # (source_week, nominal_week, max_dev)

    def rebalance_or_fund(self, ctx: WeekContext, portfolio, due: AmountsDue) -> None:
        if calendar_trigger(self.mode, ctx.prev_week, ctx.week):
            execute_rebalance(portfolio, ctx, due, TradeReason.CALENDAR_REBALANCE, self.mode,
                              ctx.week, ctx.week)
        elif self.pending is not None:
            source, nominal, dev = self.pending
            self.pending = None
            execute_rebalance(portfolio, ctx, due, TradeReason.BAND_REBALANCE, self.mode,
                              source, nominal, dev)
        elif due.total > 0:
            sell_to_pay(portfolio, ctx, due)

    def end_of_week(self, ctx: WeekContext, portfolio) -> None:
        if self.mode != "band":
            return
        breached, dev = band_breached(portfolio.ledger, ctx.targets, self.band_pp)
        if breached:
            self.pending = (ctx.week, ctx.week + WEEK, dev)


def hooks_from_config(cfg) -> StrategicHooks:
    band = cfg.get("portfolio.rebalance_band_pp")
    return StrategicHooks(cfg.get("portfolio.rebalance"), float(band) if band is not None else None)
