"""Typed, immutable data and state structures shared by all layers."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict, fields
from enum import Enum
from typing import Optional

RISKY_ASSETS = ("stocks", "gold", "btc")
ALL_SLEEVES = RISKY_ASSETS + ("rf",)
TOL_IDENTITY_REL = 1e-10  # RISK-004, interpreted as relative tolerance (Q-049)


def canonical_assets(assets) -> tuple:
    """Deterministic asset order independent of input ordering: stocks, gold, btc, then others
    alphabetically."""
    known = [a for a in RISKY_ASSETS if a in assets]
    extra = sorted(a for a in assets if a not in RISKY_ASSETS and a != "rf")
    return tuple(known + extra)


class State(str, Enum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"


class Condition(str, Enum):
    NO_SMA = "NO_SMA"
    BELOW = "BELOW_LOWER"
    INSIDE = "INSIDE"
    ABOVE = "ABOVE_UPPER"


class Severity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    role: str
    message: str
    week_key: Optional[dt.date] = None
    requirement_ids: str = ""

    def row(self) -> dict:
        return {"severity": self.severity.value, "code": self.code, "role": self.role,
                "week_key": self.week_key.isoformat() if self.week_key else "",
                "requirement_ids": self.requirement_ids, "message": self.message}


@dataclass(frozen=True)
class SourceSegment:
    source: str
    first_key: dt.date
    last_key: dt.date
    rows: int
    verified: bool = True
    note: str = ""
    rebase_factor: Optional[float] = None


@dataclass(frozen=True)
class Provenance:
    role: str
    path: str
    sha256: str
    config_key: str
    adapter: str
    canonical: bool
    raw_rows: int
    used_rows: int
    raw_first_date: Optional[dt.date]
    raw_last_date: Optional[dt.date]
    first_key: Optional[dt.date]
    last_key: Optional[dt.date]
    date_convention: str
    transformations: tuple = ()
    segments: tuple = ()          # tuple[SourceSegment]
    excluded: tuple = ()          # tuple[(raw_date_iso, reason)]
    warnings: tuple = ()
    issues: tuple = ()            # tuple[ValidationIssue]
    resolved_via_alias: bool = False
    source_label: str = ""
    extra: tuple = ()             # tuple[(key, value)] adapter specific facts
    components: tuple = ()        # tuple[Provenance] for composed series

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "issues":
                continue
            if f.name == "segments":
                v = [{k: _jsonable(x) for k, x in asdict(s).items()} for s in v]
            elif f.name == "components":
                v = [c.to_dict() for c in v]
            elif f.name == "extra":
                v = {k: _jsonable(x) for k, x in v}
            else:
                v = _jsonable(v)
            out[f.name] = v
        return out


def _jsonable(v):
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


@dataclass(frozen=True)
class PricePoint:
    week_key: dt.date
    price: float
    available_at: dt.date
    source_date: dt.date
    source: str = ""
    flags: tuple = ()


@dataclass(frozen=True)
class ReturnPoint:
    week_key: dt.date
    value: float
    available_at: dt.date
    source_date: dt.date
    flags: tuple = ()


@dataclass(frozen=True)
class PriceSeries:
    role: str
    points: tuple
    provenance: Provenance

    def keys(self) -> tuple:
        return tuple(p.week_key for p in self.points)

    def by_key(self) -> dict:
        return {p.week_key: p for p in self.points}

    def returns(self) -> tuple:
        """Weekly price returns P_t/P_{t-1}-1 assigned to week t, only between consecutive
        calendar weeks (a return spanning a gap is not a one-week return)."""
        out = []
        for a, b in zip(self.points, self.points[1:]):
            if (b.week_key - a.week_key).days == 7:
                out.append(ReturnPoint(b.week_key, b.price / a.price - 1.0, b.available_at,
                                       b.source_date, b.flags))
        return tuple(out)

    def replace_points(self, points) -> "PriceSeries":
        return PriceSeries(self.role, tuple(points), self.provenance)


@dataclass(frozen=True)
class ReturnSeries:
    role: str
    points: tuple
    provenance: Provenance

    def keys(self) -> tuple:
        return tuple(p.week_key for p in self.points)

    def by_key(self) -> dict:
        return {p.week_key: p for p in self.points}


@dataclass(frozen=True)
class FFData:
    stock_total: ReturnSeries   # (Mkt-RF + RF) / 100
    rf: ReturnSeries            # RF / 100
    provenance: Provenance


@dataclass(frozen=True)
class DividendPoint:
    week_key: dt.date
    dividend_return: float
    dividend_points: float
    spx_close_prev: float
    status: str
    available_at: dt.date


@dataclass(frozen=True)
class DividendSeries:
    points: tuple
    provenance: Provenance

    def by_key(self) -> dict:
        return {p.week_key: p for p in self.points}


@dataclass(frozen=True)
class CpiSeries:
    values: dict            # (year, month) -> float
    imputed: frozenset      # (year, month) filled by previous_available
    provenance: Provenance

    def value(self, day: dt.date) -> float:
        return self.values[(day.year, day.month)]


@dataclass(frozen=True)
class SignalParams:
    asset: str
    ma: int
    threshold_off: float
    threshold_on: float
    confirm_off: int
    confirm_on: int
    delay: int
    sell_fraction: float
    risk_off_action: str

    @property
    def minimum_warmup_weeks(self) -> int:
        """NORM-010: ma_length + max(confirm) + max(delay)."""
        return self.ma + max(self.confirm_off, self.confirm_on) + self.delay


@dataclass(frozen=True)
class ScheduledExecution:
    asset: str
    confirm_week: dt.date
    execution_week: dt.date
    target_state: State


@dataclass(frozen=True)
class SignalRecord:
    """One evaluated week of the signal pipeline (REP-006, REP-014)."""
    asset: str
    week_key: dt.date
    available_at: dt.date
    price: float
    sma: Optional[float]
    lower_band: Optional[float]
    upper_band: Optional[float]
    condition: Condition
    exit_counter: int
    entry_counter: int
    confirmed_state: State
    state_basis: str                      # fallback | confirmed
    confirmation: bool
    scheduled_execution_week: Optional[dt.date]
    effective_state: State
    executed_target: Optional[State]
    flags: tuple = ()


@dataclass(frozen=True)
class Sleeve:
    """Strategic sleeve of a risky asset: asset value + its dedicated RF reserve (PORT-009)."""
    asset: str
    asset_value: float
    reserve_value: float

    @property
    def total(self) -> float:
        return self.asset_value + self.reserve_value


@dataclass(frozen=True)
class PortfolioSleeves:
    risky: tuple            # tuple[Sleeve] in canonical order
    rf_base: float

    @property
    def nav(self) -> float:
        return sum(s.total for s in self.risky) + self.rf_base

    def sleeve(self, asset: str) -> Sleeve:
        for s in self.risky:
            if s.asset == asset:
                return s
        raise KeyError(asset)

    def weights(self) -> dict:
        """Actual sleeve weights (targets apply to sleeve totals, PORT-010)."""
        nav = self.nav
        out = {s.asset: s.total / nav for s in self.risky}
        out["rf"] = self.rf_base / nav
        return out

    def check_identity(self, nav: float) -> None:
        """RISK-004: sum(risk assets) + rf_base + sum(reserves) == NAV."""
        parts = sum(s.asset_value for s in self.risky) + self.rf_base + sum(
            s.reserve_value for s in self.risky)
        if abs(parts - nav) > TOL_IDENTITY_REL * max(1.0, abs(nav)):
            raise AssertionError(f"accounting identity broken: parts={parts!r} nav={nav!r}")
        for s in self.risky:
            if s.asset_value < 0 or s.reserve_value < 0:
                raise AssertionError(f"negative sleeve component: {s}")
        if self.rf_base < 0:
            raise AssertionError("negative rf_base")


@dataclass(frozen=True)
class RunCalendar:
    """Canonical weekly calendar actually executed by a run after source alignment (Q-012)."""
    weeks: tuple
    common_gaps: tuple          # weeks missing from every calendar-defining source
    dropped: tuple              # weeks removed by missing.return_policy=drop
    carried: tuple              # (role, week_key) price carried by missing.price_policy=carry
    issues: tuple = ()


class TradeReason(str, Enum):
    """Reason codes of every trade (REP-004): signal, calendar/band rebalance, sell_to_pay,
    terminal_liquidation (individual_pl) and foundation_distribution_liquidation (foundation
    terminal settlement); walk_forward_rebalance is reserved for walk-forward."""
    SIGNAL_EXIT = "signal_exit"
    SIGNAL_REENTRY = "signal_reentry"
    CALENDAR_REBALANCE = "calendar_rebalance"
    BAND_REBALANCE = "band_rebalance"
    SELL_TO_PAY = "sell_to_pay"
    WALK_FORWARD_REBALANCE = "walk_forward_rebalance"
    TERMINAL_LIQUIDATION = "terminal_liquidation"
    FOUNDATION_DISTRIBUTION_LIQUIDATION = "foundation_distribution_liquidation"


@dataclass(frozen=True)
class Trade:
    """One buy or sell of a risky asset against a cash-like RF component (Q-017).

    net_cash_flow is signed from the cash component's point of view: + sale proceeds after
    costs credited, - cash spent on a purchase including costs."""
    week_key: dt.date
    asset: str
    side: str                       # "sell" | "buy"
    reason: TradeReason
    gross_traded_value: float
    transaction_cost: float
    slippage: float
    net_cash_flow: float
    asset_value_before: float
    asset_value_after: float
    reserve_before: float
    reserve_after: float
    cash_component: str             # e.g. rf_reserve_stocks
    units: float
    cost_basis: float               # basis of units sold / cost of units bought
    realized_gain: Optional[float]  # sells only: net proceeds - cost basis (no tax here)
    confirm_week: Optional[dt.date] = None
    pipeline_step: Optional[int] = 1               # PORT-011 step; None in the terminal phase
    nominal_execution_week: Optional[dt.date] = None   # scheduled week of a signal execution
    phase: str = "weekly"                          # weekly (PORT-011 pipeline) | terminal


@dataclass(frozen=True)
class Payment:
    """Cash leaving the portfolio (NAV outflow), e.g. a future annual tax or foundation admin
    cost. Not a trade: no asset changes hands."""
    week_key: dt.date
    event_type: str
    amount: float
    pipeline_step: Optional[int]    # PORT-011 step; None in the terminal phase
    funding_source: str             # ledger component debited
    context: str                    # e.g. sell_to_pay:A_rf_base, strategic_rebalance
    phase: str = "weekly"           # weekly | terminal


@dataclass(frozen=True)
class RfTransfer:
    """Book transfer between cash-like RF components; costs nothing (Q-017)."""
    week_key: dt.date
    source: str
    destination: str
    amount: float
    reason: str
    pipeline_step: Optional[int]
    phase: str = "weekly"


@dataclass(frozen=True)
class RebalanceEvent:
    week_key: dt.date               # actual execution week (step 3)
    mode: str
    reason: TradeReason
    trigger_source_week: dt.date
    nominal_execution_week: dt.date
    nav_after_signal: float
    amounts_due: float
    nav_net_for_rebalance: float    # NAV_after_signal - amounts_due
    planned_final_nav: float        # after costs and payment
    realized_final_nav: float
    transaction_costs: float
    slippage: float
    weights_before: dict
    weights_after: dict
    max_deviation: Optional[float] = None   # band trigger evidence


@dataclass(frozen=True)
class DividendReinvestment:
    """Step-5 settlement of a supplied dividend return (DIV-005..007, Q-016): total-return
    accounting, not a trade (no cost, slippage, turnover or trade count)."""
    week_key: dt.date
    asset: str
    value_before_returns: float     # exposure used for the week's return
    dividend_return: float
    gross_dividend: float           # value_before_returns * dividend_return
    dividend_tax: float             # withheld from the asset
    net_reinvested: float           # new lot cost
    units: float
    unit_price: float               # price-component unit price after step 4
    lot_id: int
    pipeline_step: int = 5
