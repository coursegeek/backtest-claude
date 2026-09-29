"""Taxation of the individual_pl profile (TAX-001, TAX-004, IND-001..015, IND-018, IND-019,
DIV-002, DIV-005..007, PORT-013, Q-014, Q-015, Q-016, Q-029, Q-035).

A separate, stateful module plugged into the central PORT-011 pipeline as a hook:

  step 2  first retained week whose Friday week_key is in a new year: close every earlier
          open tax year - annual netting of realized gains/losses of stocks+gold+btc (no
          mark-to-market), loss carry-forward buckets (oldest first, 5 years, offset fraction),
          capital gains tax and solidarity tax -> ``AmountsDue`` items (paid in step 3 by the
          existing rebalance / sell_to_pay mechanism; never paid in step 2)
  step 5  immediate taxes of the week: dividend tax on the supplied dividend return
          (withheld from the stock value, net dividend reinvested as a new lot) and RF income
          tax per RF component (withheld from that component; negative RF gives no credit)
  step 6  bookkeeping: realizations are attributed to the tax year of their Friday week_key
          (Q-029) and annual payments are reconciled with their liabilities

Nothing here touches the ledger except through WorkingPortfolio primitives (settle_dividend,
withhold) and the step-3 funding policy. ``TaxState`` holds everything that must survive a
walk-forward window boundary; ``TaxParams.zero_rates()`` gives the pre-tax shadow run of Q-015
(same pipeline, every tax rate 0, transaction costs unchanged).
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

from .engine import AmountsDue, PipelineHooks, WeekContext
from .errors import ConfigError
from .models import RISKY_ASSETS, canonical_assets
from .rf import RF_COMPONENTS

# Q-035: tax_events.csv carries taxes and (later) administrative costs
EVENT_CATEGORY = {
    "dividend_tax": "tax",
    "rf_interest_tax": "tax",
    "capital_gains_tax": "tax",
    "solidarity_tax": "tax",
    "foundation_distribution_tax": "tax",
    "foundation_setup_cost": "cost",
    "foundation_annual_admin_cost": "cost",
}
ANNUAL_TYPES = ("capital_gains_tax", "solidarity_tax")
SUPPORTED_PROFILES = ("none", "individual_pl")


def event_category(event_type: str) -> Optional[str]:
    return EVENT_CATEGORY.get(event_type)


def tax_year(week: dt.date) -> int:
    """Q-029: the tax year of every weekly and transaction event is the year of its Friday
    week_key."""
    return week.year


# ============================================================================ parameters
@dataclass(frozen=True)
class TaxParams:
    profile: str = "individual_pl"
    dividend_rate: float = 0.19                  # DIV-002
    capital_gains_rate: float = 0.19             # IND-001
    solidarity_rate: float = 0.04                # IND-002
    solidarity_threshold_pln: float = 1_000_000.0    # IND-003
    external_solidarity_base_pln: float = 0.0    # IND-004
    loss_carryforward_years: int = 5             # IND-010
    loss_offset_fraction: float = 1.0            # IND-013
    rf_interest_rate: float = 0.19               # IND-014
    capital_assets: tuple = RISKY_ASSETS         # IND-019 netting

    def __post_init__(self):
        for name in ("dividend_rate", "capital_gains_rate", "solidarity_rate",
                     "loss_offset_fraction", "rf_interest_rate"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ConfigError(f"tax parameter {name}={v!r} outside 0..1")
        if self.solidarity_threshold_pln < 0 or self.external_solidarity_base_pln < 0:
            raise ConfigError("solidarity threshold and external base must be >= 0 PLN")
        if self.loss_carryforward_years < 0 or int(self.loss_carryforward_years) != self.loss_carryforward_years:
            raise ConfigError("tax.individual.loss_carryforward_years must be an integer >= 0")

    @classmethod
    def from_config(cls, cfg) -> "TaxParams":
        profile = cfg.get("tax.profile")
        if profile != "individual_pl":
            raise ConfigError(f"TaxParams for profile {profile!r}: only individual_pl is implemented")
        g = lambda k: cfg.get(f"tax.individual.{k}")                     # noqa: E731
        return cls(profile, float(g("dividend_rate")), float(g("capital_gains_rate")),
                   float(g("solidarity_rate")), float(g("solidarity_threshold_pln")),
                   float(g("external_solidarity_base_pln")), int(g("loss_carryforward_years")),
                   float(g("loss_offset_fraction")), float(g("rf_interest_rate")))

    def zero_rates(self) -> "TaxParams":
        """Q-015: the pre-tax shadow run uses the identical pipeline with every tax rate 0."""
        return dataclasses.replace(self, dividend_rate=0.0, capital_gains_rate=0.0,
                                   solidarity_rate=0.0, rf_interest_rate=0.0)


# ============================================================================ records
@dataclass(frozen=True)
class TaxEvent:
    """One tax (or future administrative cost) determination (TAX-004, REP-015, Q-035).

    ``amount`` is the tax due. Weekly (immediate) taxes are withheld directly from
    ``component`` in the same step; annual taxes become AmountsDue in step 2 and leave the
    ledger as a Payment in step 3 (never recorded twice)."""
    week_key: dt.date
    event_type: str
    category: str                   # tax | cost
    settlement: str                 # weekly | annual | terminal
    tax_year: int
    asset: str                      # stocks | gold | btc | rf | portfolio
    component: str                  # ledger component charged directly ("" when paid in step 3)
    gross_base: float
    taxable_base: float
    rate: float
    amount: float
    pipeline_step: int
    source_status: str = ""         # DIV-011: dividend status actual/estimate
    notes: str = ""


@dataclass(frozen=True)
class LossBucket:
    """IND-009/IND-010: net capital loss of tax year ``year``, usable in year+1..year+N."""
    year: int
    original: float
    remaining: float


@dataclass(frozen=True)
class AnnualLiability:
    tax_year: int
    determined_week: dt.date
    annual_realized: float          # net realized gains/losses of the capital assets
    loss_offset: float
    buckets_used: tuple             # ((bucket_year, amount), ...) oldest first
    taxable_gain: float
    new_loss_bucket: float
    capital_gains_tax: float
    solidarity_base: float
    solidarity_tax: float
    paid_week: Optional[dt.date] = None
    paid_capital_gains_tax: float = 0.0
    paid_solidarity_tax: float = 0.0


@dataclass
class TaxState:
    """Everything the tax module carries between weeks (and later between walk-forward
    windows): realizations per Friday tax year, loss buckets, annual liabilities, paid totals
    and the audit trail of tax events."""
    open_year: Optional[int] = None
    realizations: dict = field(default_factory=dict)       # year -> [(asset, gain, week)]
    loss_buckets: list = field(default_factory=list)       # LossBucket, oldest first
    expired_losses: list = field(default_factory=list)     # (bucket_year, amount, last_usable_year)
    annual_liabilities: dict = field(default_factory=dict)  # year -> AnnualLiability
    dividend_tax_paid: float = 0.0
    rf_tax_paid: float = 0.0
    capital_gains_tax_paid: float = 0.0
    solidarity_tax_paid: float = 0.0
    tax_events: list = field(default_factory=list)

    def copy(self) -> "TaxState":
        return copy.deepcopy(self)

    def realized_gain_by_year(self) -> dict:
        return {y: math.fsum(g for _, g, _ in rs) for y, rs in sorted(self.realizations.items())}

    def total_tax_paid(self) -> float:
        """MET-015/Q-035 (later summary): taxes only, administrative costs excluded."""
        return math.fsum([self.dividend_tax_paid, self.rf_tax_paid, self.capital_gains_tax_paid,
                          self.solidarity_tax_paid])

    def to_dict(self) -> dict:
        def j(v):
            if isinstance(v, dt.date):
                return v.isoformat()
            if isinstance(v, (list, tuple)):
                return [j(x) for x in v]
            return v
        return {
            "open_year": self.open_year,
            "realized_gain_by_year": {str(y): g for y, g in self.realized_gain_by_year().items()},
            "realized_gain_by_year_asset": {
                str(y): {a: math.fsum(g for x, g, _ in rs if x == a)
                         for a in canonical_assets({x for x, _, _ in rs})}
                for y, rs in sorted(self.realizations.items())},
            "loss_buckets": [dataclasses.asdict(b) for b in self.loss_buckets],
            "expired_losses": [list(e) for e in self.expired_losses],
            "annual_liabilities": {str(y): {k: j(v) for k, v in dataclasses.asdict(l).items()}
                                   for y, l in sorted(self.annual_liabilities.items())},
            "dividend_tax_paid": self.dividend_tax_paid,
            "rf_tax_paid": self.rf_tax_paid,
            "capital_gains_tax_paid": self.capital_gains_tax_paid,
            "solidarity_tax_paid": self.solidarity_tax_paid,
            "total_tax_paid": self.total_tax_paid(),
            "tax_events": len(self.tax_events),
        }


# ============================================================================ annual settlement
def close_tax_year(state: TaxState, params: TaxParams, year: int, week: dt.date) -> AnnualLiability:
    """IND-006, IND-009, IND-010, IND-013, IND-019, IND-001, IND-002, IND-005: close tax year
    ``year`` in step 2 of ``week`` (first retained week of a later year)."""
    gains = [g for a, g, _ in state.realizations.get(year, []) if a in params.capital_assets]
    annual = math.fsum(gains)                      # exact rounding: independent of order
    offset, used = 0.0, []
    if annual > 0:
        first_usable = year - params.loss_carryforward_years
        def usable(b):
            return first_usable <= b.year <= year - 1 and b.remaining > 0

        available = math.fsum(b.remaining for b in state.loss_buckets if usable(b))
        limit = min(available * params.loss_offset_fraction, annual)
        left = limit
        buckets = []
        for b in state.loss_buckets:               # oldest first
            if usable(b) and left > 0:
                take = min(b.remaining, left)
                left -= take
                used.append((b.year, take))
                b = LossBucket(b.year, b.original, b.remaining - take)
            buckets.append(b)
        state.loss_buckets = buckets
        offset = math.fsum(t for _, t in used)
    taxable = max(0.0, annual - offset)
    new_bucket = -annual if annual < 0 else 0.0
    if new_bucket > 0:
        state.loss_buckets.append(LossBucket(year, new_bucket, new_bucket))
    kept = []
    for b in state.loss_buckets:                   # usable up to b.year + N, then expired
        if b.year + params.loss_carryforward_years <= year:
            if b.remaining > 0:
                state.expired_losses.append((b.year, b.remaining, b.year + params.loss_carryforward_years))
        else:
            kept.append(b)
    state.loss_buckets = kept
    cg = taxable * params.capital_gains_rate
    sol_base = max(0.0, taxable) + params.external_solidarity_base_pln
    sol_taxable = max(0.0, sol_base - params.solidarity_threshold_pln)
    sol = params.solidarity_rate * sol_taxable
    liab = AnnualLiability(year, week, annual, offset, tuple(used), taxable, new_bucket, cg,
                           sol_base, sol)
    state.annual_liabilities[year] = liab
    used_txt = ",".join(f"{y}:{a!r}" for y, a in used) or "none"
    state.tax_events.append(TaxEvent(
        week, "capital_gains_tax", "tax", "annual", year, "portfolio", "", annual, taxable,
        params.capital_gains_rate, cg, 2,
        notes=f"net realized stocks+gold+btc={annual!r}; loss_offset={offset!r} "
              f"(buckets used {used_txt}); new_loss_bucket={new_bucket!r}; paid in step 3"))
    state.tax_events.append(TaxEvent(
        week, "solidarity_tax", "tax", "annual", year, "portfolio", "", sol_base, sol_taxable,
        params.solidarity_rate, sol, 2,
        notes=f"solidarity_base=max(0,taxable_gain {taxable!r})+external "
              f"{params.external_solidarity_base_pln!r}; threshold "
              f"{params.solidarity_threshold_pln!r}; dividends and RF income excluded; "
              "paid in step 3"))
    return liab


# ============================================================================ hooks
class IndividualTaxHooks(PipelineHooks):
    """individual_pl taxes as a pipeline extension (steps 2, 5, 6). Combine with the
    strategic funding policy through engine.ComposedHooks."""

    def __init__(self, params: TaxParams, state: Optional[TaxState] = None):
        self.params = params
        self.state = state if state is not None else TaxState()
        self._cursor = 0              # realizations of this run already attributed
        self._pay_cursor = 0
        self._due_now: list = []      # (event_type, amount, tax_year) determined this week

    # ---------------------------------------------------------------- bookkeeping
    def _ingest(self, pf) -> None:
        for r in pf.realizations[self._cursor:]:
            self.state.realizations.setdefault(tax_year(r.week_key), []).append(
                (r.asset, r.realized_gain, r.week_key))
        self._cursor = len(pf.realizations)

    def _reconcile_payments(self, ctx: WeekContext, pf) -> None:
        paid = {t: math.fsum(p.amount for p in pf.payments[self._pay_cursor:] if p.event_type == t)
                for t in ANNUAL_TYPES}
        self._pay_cursor = len(pf.payments)
        due = {t: math.fsum(a for x, a, _ in self._due_now if x == t) for t in ANNUAL_TYPES}
        for t in ANNUAL_TYPES:
            if abs(paid[t] - due[t]) > 1e-9 * max(1.0, due[t]):
                raise AssertionError(f"{ctx.week}: {t} due {due[t]!r} but paid {paid[t]!r}")
        st = self.state
        st.capital_gains_tax_paid += paid["capital_gains_tax"]
        st.solidarity_tax_paid += paid["solidarity_tax"]
        for y in sorted({y for _, _, y in self._due_now}):
            l = st.annual_liabilities[y]
            st.annual_liabilities[y] = dataclasses.replace(
                l, paid_week=ctx.week, paid_capital_gains_tax=l.capital_gains_tax,
                paid_solidarity_tax=l.solidarity_tax)
        self._due_now = []

    # ---------------------------------------------------------------- step 2
    def amounts_due(self, ctx: WeekContext, portfolio) -> AmountsDue:
        self._ingest(portfolio)
        st, year = self.state, tax_year(ctx.week)
        if st.open_year is None:
            st.open_year = year
        if year < st.open_year:
            raise ValueError(f"{ctx.week}: tax year {year} before the open year {st.open_year}")
        items = []
        while st.open_year < year:                 # usually exactly one closed year
            liab = close_tax_year(st, self.params, st.open_year, ctx.week)
            for t, a in (("capital_gains_tax", liab.capital_gains_tax),
                         ("solidarity_tax", liab.solidarity_tax)):
                if a > 0:
                    items.append((t, a))
                    self._due_now.append((t, a, liab.tax_year))
            st.open_year += 1
        if not items:
            return AmountsDue()
        return AmountsDue(math.fsum(a for _, a in items), tuple(items))

    # ---------------------------------------------------------------- step 5
    def immediate_taxes(self, ctx: WeekContext, portfolio, ledger_before_returns, market) -> None:
        self._ingest(portfolio)
        p, st, week, year = self.params, self.state, ctx.week, tax_year(ctx.week)
        for a in canonical_assets(market.dividend_yield):          # DIV-005..007, Q-016
            d = market.dividend_yield[a]
            v = ledger_before_returns.asset(a)
            if not d or v <= 0:
                continue
            gross = v * d
            tax = gross * p.dividend_rate
            rec = portfolio.settle_dividend(week, a, v, d, tax)
            if rec.dividend_tax > 0:
                st.dividend_tax_paid += rec.dividend_tax
                st.tax_events.append(TaxEvent(
                    week, "dividend_tax", "tax", "weekly", year, a, a, gross, gross,
                    p.dividend_rate, rec.dividend_tax, 5,
                    source_status=market.dividend_status.get(a, ""),
                    notes=f"gross_dividend={gross!r} (value_before_returns {v!r} x dividend_return "
                          f"{d!r}); net_reinvested={rec.net_reinvested!r} lot {rec.lot_id}"))
        r = market.rf_return                                       # IND-014/015, PORT-013
        for c in RF_COMPONENTS:
            v = getattr(ledger_before_returns, c)
            income = max(0.0, v * r)
            tax = income * p.rf_interest_rate
            if tax <= 0:
                continue                           # negative RF: no tax, no credit
            withheld = portfolio.withhold(week, c, tax)
            st.rf_tax_paid += withheld
            st.tax_events.append(TaxEvent(
                week, "rf_interest_tax", "tax", "weekly", year, "rf", c, v * r, income,
                p.rf_interest_rate, withheld, 5,
                notes=f"rf_income=max(0,{v!r}*{r!r}); withheld from {c}"))

    # ---------------------------------------------------------------- step 6
    def end_of_week(self, ctx: WeekContext, portfolio) -> None:
        self._ingest(portfolio)
        self._reconcile_payments(ctx, portfolio)


def tax_hooks_from_config(cfg) -> Optional[IndividualTaxHooks]:
    profile = cfg.get("tax.profile")
    if profile == "none":
        return None
    if profile not in SUPPORTED_PROFILES:
        raise ConfigError(f"unsupported tax profile {profile!r}")
    return IndividualTaxHooks(TaxParams.from_config(cfg))
