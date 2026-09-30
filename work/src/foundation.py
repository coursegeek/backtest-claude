"""Family foundation profiles family_foundation_15 / family_foundation_19 with
tax.foundation.tax_event = terminal (TAX-002, FND-001..016, DIV-003, Q-032, Q-033, Q-034).

A separate stateful module plugged into the central PORT-011 pipeline like the individual
tax module, sharing only the step-5 primitive and the TaxEvent audit record:

  step 0  setup cost (FND-015/016): investable = initial_capital - setup_cost, before the
          initial allocation; not a trade, no costs, not a tax (category cost)
  step 2  first retained week of a new Friday year: the annual admin cost of every closed year
          (FND-010/011/012/014, Q-034 proration over (inception_date, last_week]) becomes an
          AmountsDue item paid in step 3 by the existing rebalance / sell_to_pay funding
  step 5  foundation dividend tax (15%, same Q-016 economics as individual_pl) and RF income
          tax at tax.foundation.rf_interest_rate (default 0)
  step 6  realizations are audited per Friday tax year (internal trading tax rate 0,
          FND-002/TEST-015); admin payments reconciled
The final-year admin cost and the distribution tax belong to the terminal settlement
(settlement.settle_foundation_terminal). ``FoundationState`` holds everything that must
survive a walk-forward window boundary; it has no loss buckets or capital gains semantics.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

from .engine import AmountsDue, PipelineHooks, WeekContext
from .errors import ConfigError, NotImplementedCommand
from .tax import TaxEvent, charge_immediate_taxes, tax_year

FOUNDATION_PROFILES = ("family_foundation_15", "family_foundation_19")
ADMIN_COST = "foundation_annual_admin_cost"
SETUP_COST = "foundation_setup_cost"
DISTRIBUTION_TAX = "foundation_distribution_tax"


# ============================================================================ parameters
@dataclass(frozen=True)
class FoundationParams:
    profile: str
    dividend_rate: float = 0.15                    # FND-001
    rf_interest_rate: float = 0.0                  # FND-013
    internal_trading_tax_rate: float = 0.0         # FND-002 (only 0 supported, Q-047)
    distribution_rate: float = 0.15                # FND-003 / FND-004
    distribution_tax_base: str = "distributed_amount"   # FND-006
    tax_event: str = "terminal"                    # FND-005 (distribution_schedule: Q-037)
    setup_cost_pln: float = 40_000.0               # FND-015
    annual_admin_cost_pln: float = 40_000.0        # FND-010
    admin_cost_proration: str = "prorated"         # FND-012

    def __post_init__(self):
        if self.profile not in FOUNDATION_PROFILES:
            raise ConfigError(f"foundation profile {self.profile!r} not in {FOUNDATION_PROFILES}")
        for name in ("dividend_rate", "rf_interest_rate", "internal_trading_tax_rate",
                     "distribution_rate"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ConfigError(f"foundation parameter {name}={v!r} outside 0..1")
        if self.setup_cost_pln < 0 or self.annual_admin_cost_pln < 0:
            raise ConfigError("foundation setup and admin costs must be >= 0 PLN")
        if self.distribution_tax_base not in ("distributed_amount", "gain_only"):
            raise ConfigError(f"tax.foundation.distribution_tax_base={self.distribution_tax_base!r}")
        if self.admin_cost_proration not in ("prorated", "full"):
            raise ConfigError(f"tax.foundation.admin_cost_proration={self.admin_cost_proration!r}")
        if self.tax_event == "distribution_schedule":
            raise NotImplementedCommand("tax.foundation.tax_event=distribution_schedule: "
                                        "distribution_schedule is not implemented; Q-037 remains open")
        if self.tax_event != "terminal":
            raise ConfigError(f"tax.foundation.tax_event={self.tax_event!r}")
        if self.internal_trading_tax_rate > 0:
            raise NotImplementedCommand(
                f"tax.foundation.internal_trading_tax_rate={self.internal_trading_tax_rate!r}: a "
                "non-zero internal trading tax is not implemented (settlement semantics open, "
                "Q-047); only the default 0 is supported")

    @classmethod
    def from_config(cls, cfg) -> "FoundationParams":
        profile = cfg.get("tax.profile")
        if profile not in FOUNDATION_PROFILES:
            raise ConfigError(f"FoundationParams for profile {profile!r}")
        g = lambda k: cfg.get(f"tax.foundation.{k}")                      # noqa: E731
        rate_key = "tax.foundation_15.distribution_rate" if profile.endswith("15") \
            else "tax.foundation_19.distribution_rate"
        return cls(profile, float(g("dividend_rate")), float(g("rf_interest_rate")),
                   float(g("internal_trading_tax_rate")), float(cfg.get(rate_key)),
                   g("distribution_tax_base"), g("tax_event"), float(g("setup_cost_pln")),
                   float(g("annual_admin_cost_pln")), g("admin_cost_proration"))

    def zero_rates(self) -> "FoundationParams":
        """Q-015: pre-tax shadow - every tax rate 0; setup and admin costs are costs and stay."""
        return dataclasses.replace(self, dividend_rate=0.0, rf_interest_rate=0.0,
                                   internal_trading_tax_rate=0.0, distribution_rate=0.0)


# ============================================================================ proration
def days_in_year(year: int) -> int:
    return 366 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 365


def active_days_in_year(year: int, inception: dt.date, last_day: dt.date) -> int:
    """Q-034: calendar days of ``year`` inside (inception_date, last_day] (inception excluded,
    last day included)."""
    lo = max(inception + dt.timedelta(days=1), dt.date(year, 1, 1))
    hi = min(last_day, dt.date(year, 12, 31))
    return max(0, (hi - lo).days + 1)


def admin_cost_for_year(params: FoundationParams, year: int, inception: dt.date,
                        last_day: dt.date) -> tuple:
    """FND-010/012: (amount, active_days, days_in_year, factor). prorated: annual * active /
    days_in_year (365/366); full: the annual cost for every year with an active day."""
    active, days = active_days_in_year(year, inception, last_day), days_in_year(year)
    if params.admin_cost_proration == "full":
        factor = 1.0 if active > 0 else 0.0
    else:
        factor = active / days
    return params.annual_admin_cost_pln * factor, active, days, factor


# ============================================================================ state
@dataclass(frozen=True)
class AdminCost:
    year: int
    active_days: int
    days_in_year: int
    factor: float
    amount: float
    determined_week: dt.date
    settlement: str                 # annual (step 2 of Y+1) | terminal (final year)
    paid_week: Optional[dt.date] = None


@dataclass
class FoundationState:
    """Transferable foundation state (later walk-forward): costs, taxes, audited realizations
    per Friday year and the event trail. No loss buckets, no capital gains tax."""
    open_year: Optional[int] = None
    setup_cost_paid: float = 0.0
    admin_cost_paid: float = 0.0
    admin_cost_by_year: dict = field(default_factory=dict)     # year -> AdminCost
    closed_admin_years: list = field(default_factory=list)
    dividend_tax_paid: float = 0.0
    rf_tax_paid: float = 0.0
    internal_trading_tax_paid: float = 0.0
    distribution_tax_paid: float = 0.0
    realizations: dict = field(default_factory=dict)          # year -> [(asset, gain, week)]
    tax_events: list = field(default_factory=list)

    def copy(self) -> "FoundationState":
        return copy.deepcopy(self)

    def total_tax_paid(self) -> float:
        """MET-015/Q-035: taxes only (setup/admin costs are category cost)."""
        return math.fsum([self.dividend_tax_paid, self.rf_tax_paid, self.internal_trading_tax_paid,
                          self.distribution_tax_paid])

    def admin_cost_paid_by(self, settlement: str) -> float:
        return math.fsum(c.amount for c in self.admin_cost_by_year.values()
                         if c.settlement == settlement and c.paid_week is not None)

    def totals(self) -> dict:
        return {"total_tax_paid": self.total_tax_paid(), "dividend_tax_paid": self.dividend_tax_paid,
                "rf_interest_tax_paid": self.rf_tax_paid, "capital_gains_tax_paid": 0.0,
                "solidarity_tax_paid": 0.0, "internal_trading_tax_paid": self.internal_trading_tax_paid,
                "foundation_distribution_tax_paid": self.distribution_tax_paid,
                "foundation_setup_cost_paid": self.setup_cost_paid,
                "foundation_admin_cost_paid": self.admin_cost_paid,
                "foundation_admin_cost_weekly": self.admin_cost_paid_by("annual"),
                "foundation_admin_cost_terminal": self.admin_cost_paid_by("terminal")}

    def to_dict(self) -> dict:
        def j(v):
            return v.isoformat() if isinstance(v, dt.date) else v
        return {
            "open_year": self.open_year, "setup_cost_paid": self.setup_cost_paid,
            "admin_cost_paid": self.admin_cost_paid,
            "admin_cost_by_year": {str(y): {k: j(v) for k, v in dataclasses.asdict(c).items()}
                                   for y, c in sorted(self.admin_cost_by_year.items())},
            "closed_admin_years": list(self.closed_admin_years),
            "dividend_tax_paid": self.dividend_tax_paid, "rf_tax_paid": self.rf_tax_paid,
            "internal_trading_tax_paid": self.internal_trading_tax_paid,
            "distribution_tax_paid": self.distribution_tax_paid,
            "total_tax_paid": self.total_tax_paid(),
            "realized_gain_by_year": {str(y): math.fsum(g for _, g, _ in rs)
                                      for y, rs in sorted(self.realizations.items())},
            "tax_events": len(self.tax_events),
        }


def admin_cost_event(params: FoundationParams, cost: AdminCost, settlement: str) -> TaxEvent:
    step, phase = (2, "weekly") if settlement == "annual" else (None, "terminal")
    return TaxEvent(
        cost.determined_week, ADMIN_COST, "cost", settlement, cost.year, "portfolio", "",
        params.annual_admin_cost_pln, params.annual_admin_cost_pln, cost.factor, cost.amount, step,
        notes=f"{params.admin_cost_proration}: {cost.active_days} active days of {cost.days_in_year} "
              f"in (inception, last week] (Q-034); "
              + ("paid in step 3" if settlement == "annual" else "paid in the terminal settlement"),
        phase=phase)


# ============================================================================ hooks
class FoundationHooks(PipelineHooks):
    """Foundation profiles as a pipeline extension (steps 0, 2, 5, 6); combine with the
    strategic funding policy through engine.ComposedHooks."""

    def __init__(self, params: FoundationParams, inception: dt.date,
                 state: Optional[FoundationState] = None):
        self.params = params
        self.inception = inception
        self.state = state if state is not None else FoundationState()
        self._cursor = 0
        self._pay_cursor = 0
        self._due_now: list = []

    # ---------------------------------------------------------------- step 0
    def investable_capital(self, capital: float) -> float:
        """FND-015/016, Q-033: setup cost before the initial allocation."""
        setup = self.params.setup_cost_pln
        if setup >= capital:
            raise ConfigError(f"tax.foundation.setup_cost_pln={setup!r} >= initial capital "
                              f"{capital!r}: nothing left to invest (FND-015)")
        if setup > 0:
            self.state.setup_cost_paid += setup
            self.state.tax_events.append(TaxEvent(
                self.inception, SETUP_COST, "cost", "initial", self.inception.year, "portfolio", "",
                capital, setup, 1.0, setup, 0,
                notes="one-off setup cost deducted before the initial allocation; not a trade, "
                      "no transaction costs, not a tax; the weekly path starts at "
                      f"{capital - setup!r}", phase="initialization"))
        return capital - setup

    # ---------------------------------------------------------------- bookkeeping
    def _ingest(self, pf) -> None:
        for r in pf.realizations[self._cursor:]:          # FND-002: audited, rate 0 -> no tax
            self.state.realizations.setdefault(tax_year(r.week_key), []).append(
                (r.asset, r.realized_gain, r.week_key))
        self._cursor = len(pf.realizations)

    # ---------------------------------------------------------------- step 2
    def amounts_due(self, ctx: WeekContext, portfolio) -> AmountsDue:
        self._ingest(portfolio)
        st, year = self.state, tax_year(ctx.week)
        if st.open_year is None:
            st.open_year = self.inception.year           # active from the day after inception
        items = []
        while st.open_year < year:
            y = st.open_year
            amount, active, days, factor = admin_cost_for_year(self.params, y, self.inception,
                                                               ctx.week)
            cost = AdminCost(y, active, days, factor, amount, ctx.week, "annual")
            st.admin_cost_by_year[y] = cost
            st.closed_admin_years.append(y)
            st.tax_events.append(admin_cost_event(self.params, cost, "annual"))
            if amount > 0:
                items.append((ADMIN_COST, amount))
                self._due_now.append((y, amount))
            st.open_year += 1
        if not items:
            return AmountsDue()
        return AmountsDue(math.fsum(a for _, a in items), tuple(items))

    # ---------------------------------------------------------------- step 5
    def immediate_taxes(self, ctx: WeekContext, portfolio, ledger_before_returns, market) -> None:
        self._ingest(portfolio)
        st = self.state
        for e in charge_immediate_taxes(portfolio, ctx.week, ledger_before_returns, market,
                                        self.params.dividend_rate, self.params.rf_interest_rate):
            if e.event_type == "dividend_tax":
                st.dividend_tax_paid += e.amount
            else:
                st.rf_tax_paid += e.amount
            st.tax_events.append(e)

    # ---------------------------------------------------------------- step 6
    def end_of_week(self, ctx: WeekContext, portfolio) -> None:
        self._ingest(portfolio)
        paid = math.fsum(p.amount for p in portfolio.payments[self._pay_cursor:]
                         if p.event_type == ADMIN_COST)
        self._pay_cursor = len(portfolio.payments)
        due = math.fsum(a for _, a in self._due_now)
        if abs(paid - due) > 1e-9 * max(1.0, due):
            raise AssertionError(f"{ctx.week}: admin cost due {due!r} but paid {paid!r}")
        self.state.admin_cost_paid += paid
        for y, _ in self._due_now:
            self.state.admin_cost_by_year[y] = dataclasses.replace(
                self.state.admin_cost_by_year[y], paid_week=ctx.week)
        self._due_now = []


def foundation_hooks_from_config(cfg, inception: dt.date) -> Optional[FoundationHooks]:
    if cfg.get("tax.profile") not in FOUNDATION_PROFILES:
        return None
    return FoundationHooks(FoundationParams.from_config(cfg), inception)
