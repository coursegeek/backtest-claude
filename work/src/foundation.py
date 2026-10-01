"""Family foundation profiles family_foundation_15 / family_foundation_19 (TAX-002,
FND-001..016, DIV-003, Q-032, Q-033, Q-034, Q-037, Q-047).

A separate stateful module plugged into the central PORT-011 pipeline like the individual
tax module, sharing only the step-5 primitive and the TaxEvent audit record:

  step 0  setup cost (FND-015/016): investable = initial_capital - setup_cost, before the
          initial allocation; not a trade, no costs, not a tax (category cost)
  step 2  first retained week of a new Friday year: the annual admin cost of every closed year
          (FND-010/011/012/014, Q-034 proration over (inception_date, last_week]) and - with a
          non-zero tax.foundation.internal_trading_tax_rate (FND-002, Q-047) - the internal
          trading tax of the closed year (rate x max(0, net realized gains of the year), no loss
          carry-forward) become AmountsDue items paid in step 3 by the existing rebalance /
          sell_to_pay funding; with tax_event=distribution_schedule (FND-005/009, Q-037) every
          scheduled distribution of the week: gross D (amount, or percent_nav x NAV_after_signal)
          split into the distribution tax T and the net payout D - T (NAV outflow = D)
  step 5  foundation dividend tax (15%, same Q-016 economics as individual_pl) and RF income
          tax at tax.foundation.rf_interest_rate (default 0)
  step 6  realizations are audited per Friday tax year; payments reconciled
The final year (internal trading tax, admin cost, terminal distribution tax or - in schedule
mode - only the admin cost) belongs to the settlement after the weekly path
(settlement.settle_foundation). ``FoundationState`` holds everything that must survive a
walk-forward window boundary; it has no loss buckets or capital gains semantics.
"""
from __future__ import annotations

import bisect
import copy
import dataclasses
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

from .calendar import friday_key
from .engine import AmountsDue, PipelineHooks, WeekContext
from .errors import ConfigError
from .models import TradeReason
from .tax import TaxEvent, charge_immediate_taxes, tax_year

FOUNDATION_PROFILES = ("family_foundation_15", "family_foundation_19")
ADMIN_COST = "foundation_annual_admin_cost"
SETUP_COST = "foundation_setup_cost"
DISTRIBUTION_TAX = "foundation_distribution_tax"
DISTRIBUTION_NET = "foundation_distribution_net"       # net payout to the beneficiary (no tax)
DISTRIBUTION_GROSS = "foundation_distribution_gross"   # funding item; recorded as tax + net
INTERNAL_TAX = "foundation_internal_trading_tax"
SCHEDULE = "distribution_schedule"
COMBINATION_ERROR = ("non-zero foundation internal trading tax with distribution_schedule is not "
                     "defined by clean-room specification adjudication (Q-037/Q-047)")


# ============================================================================ parameters
@dataclass(frozen=True)
class FoundationParams:
    profile: str
    dividend_rate: float = 0.15                    # FND-001
    rf_interest_rate: float = 0.0                  # FND-013
    internal_trading_tax_rate: float = 0.0         # FND-002 (annual, Q-047)
    distribution_rate: float = 0.15                # FND-003 / FND-004
    distribution_tax_base: str = "distributed_amount"   # FND-006
    tax_event: str = "terminal"                    # FND-005: terminal | distribution_schedule
    setup_cost_pln: float = 40_000.0               # FND-015
    annual_admin_cost_pln: float = 40_000.0        # FND-010
    admin_cost_proration: str = "prorated"         # FND-012
    distribution_file: Optional[str] = None        # FND-009 (required by distribution_schedule)

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
        if self.tax_event not in ("terminal", SCHEDULE):
            raise ConfigError(f"tax.foundation.tax_event={self.tax_event!r} not in "
                              f"terminal, {SCHEDULE} (FND-005)")
        if self.tax_event == SCHEDULE:
            if not self.distribution_file:
                raise ConfigError(f"tax.foundation.tax_event={SCHEDULE} requires "
                                  "tax.foundation.distribution_file (--distribution-file); no "
                                  "fallback to terminal (FND-009, Q-037)")
            if self.internal_trading_tax_rate > 0:
                raise ConfigError(f"{COMBINATION_ERROR}: tax.foundation.internal_trading_tax_rate="
                                  f"{self.internal_trading_tax_rate!r}")

    @property
    def schedule_mode(self) -> bool:
        return self.tax_event == SCHEDULE

    @property
    def internal_tax_active(self) -> bool:
        return self.internal_trading_tax_rate > 0

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
                   float(g("annual_admin_cost_pln")), g("admin_cost_proration"),
                   g("distribution_file") or None)

    def zero_rates(self) -> "FoundationParams":
        """Q-015: pre-tax shadow - every tax rate 0 (dividend, RF, internal trading,
        distribution); setup and admin costs are costs and stay; a distribution schedule stays
        (its gross distributions leave the shadow portfolio too, untaxed)."""
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


@dataclass(frozen=True)
class InternalTaxLiability:
    """FND-002 / Q-047: internal trading tax of one Friday year (no carry-forward)."""
    year: int
    annual_realized: float          # net realized gains/losses of stocks, gold and BTC in the year
    taxable_gain: float             # max(0, annual_realized)
    rate: float
    tax: float
    determined_week: dt.date
    settlement: str                 # annual (step 2 of Y+1) | terminal (final year)
    paid_week: Optional[dt.date] = None


@dataclass(frozen=True)
class DistributionEvent:
    """Q-037: one scheduled gross distribution D = distribution tax T + net payout D - T."""
    row_index: int
    scheduled_date: dt.date
    nominal_week: dt.date
    actual_week: dt.date
    kind: str                       # amount | percent_nav
    value: float                    # the file value (PLN or decimal fraction)
    nav_base: Optional[float]       # NAV_after_signal for percent_nav rows
    gross: float
    tax_base_mode: str
    tax_base: float
    rate: float
    tax: float
    net: float
    basis_before: float             # distribution_capital_basis_remaining before the row
    basis_after: float
    paid_week: Optional[dt.date] = None


@dataclass
class FoundationState:
    """Transferable foundation state (walk-forward): costs, taxes, audited realizations per
    Friday year, internal trading tax per closed year, scheduled distributions and the event
    trail. No loss buckets, no capital gains tax."""
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
    internal_tax_by_year: dict = field(default_factory=dict)  # year -> InternalTaxLiability
    closed_internal_tax_years: list = field(default_factory=list)
    gross_distributions_paid: float = 0.0
    net_distributions_paid: float = 0.0
    distribution_capital_basis_remaining: Optional[float] = None   # Q-037 gain_only basis
    distribution_events: list = field(default_factory=list)  # DistributionEvent (paid)

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
                "foundation_admin_cost_terminal": self.admin_cost_paid_by("terminal"),
                "foundation_gross_distributions_paid": self.gross_distributions_paid,
                "foundation_net_distributions_paid": self.net_distributions_paid}

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
            "internal_tax_by_year": {str(y): {k: j(v) for k, v in dataclasses.asdict(c).items()}
                                     for y, c in sorted(self.internal_tax_by_year.items())},
            "closed_internal_tax_years": list(self.closed_internal_tax_years),
            "gross_distributions_paid": self.gross_distributions_paid,
            "net_distributions_paid": self.net_distributions_paid,
            "distribution_capital_basis_remaining": self.distribution_capital_basis_remaining,
            "distribution_events": len(self.distribution_events),
        }


def internal_tax_liability(params: "FoundationParams", state: FoundationState, year: int,
                           week: dt.date, settlement: str) -> InternalTaxLiability:
    """FND-002 / Q-047: tax = rate x max(0, net realized gains of ``year``) - gains and losses of
    stocks, gold and BTC netted inside the year, no loss carry-forward, no solidarity tax."""
    annual = math.fsum(g for _, g, _ in state.realizations.get(year, ()))
    taxable = max(0.0, annual)
    return InternalTaxLiability(year, annual, taxable, params.internal_trading_tax_rate,
                                taxable * params.internal_trading_tax_rate, week, settlement)


def internal_tax_event(params: "FoundationParams", liab: InternalTaxLiability) -> TaxEvent:
    annual = liab.settlement == "annual"
    return TaxEvent(
        liab.determined_week, INTERNAL_TAX, "tax", liab.settlement, liab.year, "portfolio", "",
        liab.annual_realized, liab.taxable_gain, liab.rate, liab.tax, 2 if annual else None,
        notes=f"{params.profile}: net realized gains of {liab.year} = {liab.annual_realized!r}; "
              "taxable = max(0, net), no loss carry-forward (Q-047); "
              + ("determined in step 2 of the first retained week of the next year, paid in step 3"
                 if annual else "final year incl. the terminal liquidation, paid in the terminal "
                 "settlement before the admin cost and the distribution"),
        phase="weekly" if annual else "terminal")


def distribution_tax_base(mode: str, gross: float, basis_before: float) -> tuple:
    """FND-006 / Q-037: (tax_base, basis_after). distributed_amount: the gross distribution;
    gain_only: the part above the remaining capital basis (cumulative over all distributions -
    capital_return = min(D, basis), base = D - capital_return)."""
    capital_return = min(gross, max(0.0, basis_before))
    basis_after = basis_before - capital_return
    if mode == "gain_only":
        return gross - capital_return, basis_after
    return gross, basis_after


def map_distribution_schedule(rows, weeks) -> tuple:
    """Q-037: scheduled_date -> Friday key of its Monday-Sunday week (nominal week); a nominal
    week removed from the retained calendar runs in the next retained week. Rows before the
    first or after the last retained week are ignored (returned with the reason).
    Returns ({actual_week: (rows sorted by scheduled_date, row index)}, ((row, reason), ...))."""
    weeks = tuple(weeks)
    by_week, ignored = {}, []
    for r in sorted(rows, key=lambda x: x.sort_key()):
        nominal = friday_key(r.scheduled_date)
        if not weeks or nominal < weeks[0]:
            ignored.append((dataclasses.replace(r, nominal_week=nominal), "before the first run week"))
            continue
        if nominal > weeks[-1]:
            ignored.append((dataclasses.replace(r, nominal_week=nominal), "after the last run week"))
            continue
        actual = weeks[bisect.bisect_left(weeks, nominal)]
        by_week.setdefault(actual, []).append(dataclasses.replace(r, nominal_week=nominal,
                                                                  actual_week=actual))
    return {w: tuple(v) for w, v in by_week.items()}, tuple(ignored)


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
    strategic funding policy through engine.ComposedHooks. ``distributions`` (schedule mode):
    {actual retained week: mapped ScheduledDistribution rows} of this run / OOS segment."""

    def __init__(self, params: FoundationParams, inception: dt.date,
                 state: Optional[FoundationState] = None, distributions: Optional[dict] = None):
        self.params = params
        self.inception = inception
        self.state = state if state is not None else FoundationState()
        self.distributions = distributions or {}
        if self.distributions and not params.schedule_mode:
            raise ConfigError("scheduled distributions need tax.foundation.tax_event="
                              f"{SCHEDULE}")
        self._cursor = 0
        self._pay_cursor = 0
        self._due_now: list = []                 # (year, admin amount)
        self._internal_due: list = []            # (year, internal tax)
        self._dist_due: list = []                # DistributionEvent (unpaid)

    # ---------------------------------------------------------------- step 0
    def investable_capital(self, capital: float) -> float:
        """FND-015/016, Q-033: setup cost before the initial allocation. Schedule mode: the
        gain_only capital basis starts at the initial capital before the setup cost (Q-037)."""
        setup = self.params.setup_cost_pln
        if setup >= capital:
            raise ConfigError(f"tax.foundation.setup_cost_pln={setup!r} >= initial capital "
                              f"{capital!r}: nothing left to invest (FND-015)")
        if self.params.schedule_mode and self.state.distribution_capital_basis_remaining is None:
            self.state.distribution_capital_basis_remaining = capital
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
        for r in pf.realizations[self._cursor:]:          # FND-002: realizations per Friday year
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
            if self.params.internal_tax_active:           # Q-047: closed year, also when 0
                liab = internal_tax_liability(self.params, st, y, ctx.week, "annual")
                st.internal_tax_by_year[y] = liab
                st.closed_internal_tax_years.append(y)
                st.tax_events.append(internal_tax_event(self.params, liab))
                if liab.tax > 0:
                    items.append((INTERNAL_TAX, liab.tax))
                    self._internal_due.append((y, liab.tax))
            st.open_year += 1
        rows = self.distributions.get(ctx.week, ())
        nav_after_signal = portfolio.ledger.nav
        for row in rows:                                  # Q-037: gross D = T + (D - T)
            ev = self._determine_distribution(row, ctx.week, nav_after_signal)
            if ev.gross > 0:                              # one funding item per row: the
                items.append((DISTRIBUTION_GROSS, ev.gross))   # ledger sees D, whatever T is
            self._dist_due.append(ev)
        if not items:
            return AmountsDue()
        reason = TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION if rows else None
        return AmountsDue(math.fsum(a for _, a in items), tuple(items), reason)

    def _determine_distribution(self, row, week: dt.date, nav_after_signal: float) -> DistributionEvent:
        p, st = self.params, self.state
        gross = row.amount if row.amount is not None else row.percent_nav * nav_after_signal
        before = st.distribution_capital_basis_remaining
        if before is None:
            raise ConfigError("distribution capital basis not initialised (schedule mode)")
        base, after = distribution_tax_base(p.distribution_tax_base, gross, before)
        st.distribution_capital_basis_remaining = after
        tax = base * p.distribution_rate
        ev = DistributionEvent(row.row_index, row.scheduled_date, row.nominal_week, week, row.kind,
                               row.amount if row.amount is not None else row.percent_nav,
                               nav_after_signal if row.percent_nav is not None else None,
                               gross, p.distribution_tax_base, base, p.distribution_rate, tax,
                               gross - tax, before, after)
        st.tax_events.append(TaxEvent(
            week, DISTRIBUTION_TAX, "tax", "scheduled", tax_year(week), "portfolio", "", gross,
            base, p.distribution_rate, tax, 2,
            notes=f"{p.profile}: scheduled distribution row {row.row_index} ({row.scheduled_date}, "
                  f"nominal week {row.nominal_week}, {row.kind}="
                  f"{row.amount if row.amount is not None else row.percent_nav!r}); gross {gross!r} "
                  f"leaves the NAV, tax withheld from it, net payout {gross - tax!r}; base="
                  f"{p.distribution_tax_base} (capital basis {before!r} -> {after!r})",
            phase="weekly"))
        return ev

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
        new = portfolio.payments[self._pay_cursor:]
        paid = {t: math.fsum(p.amount for p in new if p.event_type == t)
                for t in (ADMIN_COST, INTERNAL_TAX, DISTRIBUTION_GROSS)}
        due = {ADMIN_COST: math.fsum(a for _, a in self._due_now),
               INTERNAL_TAX: math.fsum(a for _, a in self._internal_due),
               DISTRIBUTION_GROSS: math.fsum(e.gross for e in self._dist_due)}
        for t in due:
            if abs(paid[t] - due[t]) > 1e-9 * max(1.0, due[t]):
                raise AssertionError(f"{ctx.week}: {t} due {due[t]!r} but paid {paid[t]!r}")
        st = self.state
        st.admin_cost_paid += paid[ADMIN_COST]
        for y, _ in self._due_now:
            st.admin_cost_by_year[y] = dataclasses.replace(st.admin_cost_by_year[y],
                                                           paid_week=ctx.week)
        st.internal_trading_tax_paid += paid[INTERNAL_TAX]
        for y, _ in self._internal_due:
            st.internal_tax_by_year[y] = dataclasses.replace(st.internal_tax_by_year[y],
                                                             paid_week=ctx.week)
        self._split_distribution_payments(portfolio)
        self._pay_cursor = len(portfolio.payments)
        for ev in self._dist_due:
            st.distribution_tax_paid += ev.tax
            st.net_distributions_paid += ev.net
            st.gross_distributions_paid += ev.gross
            st.distribution_events.append(dataclasses.replace(ev, paid_week=ctx.week))
        self._due_now, self._internal_due, self._dist_due = [], [], []

    def _split_distribution_payments(self, portfolio) -> None:
        """Every funded piece of a gross distribution becomes a distribution-tax and a net-payout
        Payment record (rows in funding order; the last piece of a row takes the remainder so
        that each row's parts sum exactly to its tax T and net D - T)."""
        rows = [[e.gross, e.tax] for e in self._dist_due]      # [gross left, tax left]
        r, i = 0, self._pay_cursor
        while i < len(portfolio.payments):
            p = portfolio.payments[i]
            if p.event_type != DISTRIBUTION_GROSS:
                i += 1
                continue
            while rows[r][0] <= 1e-12 * max(1.0, self._dist_due[r].gross):
                r += 1
            gross_left, tax_left = rows[r]
            last = p.amount >= gross_left * (1 - 1e-12)
            tax = tax_left if last else p.amount * self._dist_due[r].tax / self._dist_due[r].gross
            tax = min(tax, p.amount)
            rows[r] = [gross_left - p.amount, tax_left - tax]
            portfolio.split_payment(i, ((DISTRIBUTION_TAX, tax), (DISTRIBUTION_NET, p.amount - tax)))
            i += sum(1 for a in (tax, p.amount - tax) if a > 0)


def foundation_hooks_from_config(cfg, inception: dt.date) -> Optional[FoundationHooks]:
    if cfg.get("tax.profile") not in FOUNDATION_PROFILES:
        return None
    return FoundationHooks(FoundationParams.from_config(cfg), inception)
