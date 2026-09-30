"""Performance and risk metrics (MET-001..021, MET-024..026, REAL-001..004, Q-014, Q-015,
Q-040, Q-045).

Pure functions on weekly paths; no I/O, no engine state. Conventions:
  * NAV path for drawdowns = [NAV_start, nav_end week 1, ..., nav_end last week]; the running
    maximum includes NAV_start; terminal settlement is never part of any path (MET-026).
  * CAGR = (wealth / growth_base_nav) ** (365.2425 / elapsed_days) - 1 with
    elapsed_days = last retained week - inception date (Q-014). Two bases (Q-033):
    growth_base_nav (CAGR, real CAGR) = initial_capital_pln before any foundation setup cost;
    path_start_nav (weekly path, drawdown, calendar years) = NAV of the initial allocation
    (investable capital). They are equal for none and individual_pl.
  * volatility = sample std (ddof=1) of weekly returns * sqrt(52); Sharpe on weekly excess
    returns over RF, mean / sample std * sqrt(52); Sortino with the weekly MAR
    (1 + MAR_annual) ** (1/52) - 1 and the downside deviation over all weeks.
  * Calmar = CAGR / max drawdown of the weekly path; after-tax Calmar = after-tax CAGR (terminal
    settlement included) / max drawdown of the after-tax weekly path (terminal excluded).
  * calendar years by Friday week_key; the first and last year may be partial (flagged).
  * trade count and turnover: weekly-phase trades only (no terminal liquidation, no RF
    transfers, payments or dividend reinvestments); turnover = sum |gross traded value| /
    mean weekly nav_end.
Sums use math.fsum (exactly rounded, independent of order).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

DAYS_PER_YEAR = 365.2425
WEEKS_PER_YEAR = 52
US_CPI_LABEL = "US CPI-U / CPIAUCNS"
US_CPI_WARNING = "Real returns deflated by US CPI; not Polish CPI"


# ============================================================================ basics
def mean(xs) -> float:
    xs = list(xs)
    return math.fsum(xs) / len(xs)


def sample_std(xs) -> Optional[float]:
    xs = list(xs)
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def cagr(nav_end: float, nav_start: float, elapsed_days: float) -> Optional[float]:
    """MET-003/MET-004."""
    if elapsed_days <= 0 or nav_start <= 0:
        return None
    ratio = nav_end / nav_start
    if ratio <= 0:
        return -1.0
    return ratio ** (DAYS_PER_YEAR / elapsed_days) - 1.0


def annualized_volatility(returns) -> Optional[float]:
    """MET-006: sample_std(weekly returns, ddof=1) * sqrt(52)."""
    s = sample_std(returns)
    return None if s is None else s * math.sqrt(WEEKS_PER_YEAR)


def sharpe(returns, rf_returns) -> Optional[float]:
    """MET-007: x_t = R_t - RF_t; mean(x) / sample_std(x, ddof=1) * sqrt(52)."""
    x = [r - f for r, f in zip(returns, rf_returns, strict=True)]
    s = sample_std(x)
    if not s:
        return None
    return mean(x) / s * math.sqrt(WEEKS_PER_YEAR)


def sortino(returns, mar_annual: float = 0.0) -> Optional[float]:
    """MET-008: excess over the weekly MAR, downside deviation over all weeks."""
    returns = list(returns)
    if not returns:
        return None
    mar_w = (1.0 + mar_annual) ** (1.0 / WEEKS_PER_YEAR) - 1.0
    excess = [r - mar_w for r in returns]
    downside = math.sqrt(math.fsum(min(e, 0.0) ** 2 for e in excess) / len(excess))
    if downside == 0.0:
        return None
    return mean(excess) / downside * math.sqrt(WEEKS_PER_YEAR)


def nav_path(nav_start: float, nav_ends) -> list:
    return [nav_start] + list(nav_ends)


def drawdowns(path) -> list:
    """DD_t = NAV_t / running_max(NAV)_t - 1 (the running maximum includes NAV_start)."""
    out, peak = [], None
    for v in path:
        peak = v if peak is None else max(peak, v)
        out.append(v / peak - 1.0)
    return out


def max_drawdown(path) -> float:
    """MET-009: reported as the positive value abs(min(DD_t))."""
    return abs(min(drawdowns(path)))


def calmar(cagr_value: Optional[float], max_dd: float) -> Optional[float]:
    """MET-010: CAGR / max drawdown (None when there is no drawdown)."""
    if cagr_value is None or not max_dd:
        return None
    return cagr_value / max_dd


def rf_after_tax(rf_returns, rate: float) -> list:
    """Q-040: R_RF_after = R_RF - max(R_RF, 0) * rate (rate 0 for tax.profile=none)."""
    return [r - max(r, 0.0) * rate for r in rf_returns]


def real_cagr(wealth: float, nav_start: float, cpi_start: float, cpi_end: float,
              elapsed_days: float) -> Optional[float]:
    """REAL-001/MET-005: real_growth = (wealth / NAV_start) / (CPI_end / CPI_start)."""
    if not cpi_start or not cpi_end or elapsed_days <= 0:
        return None
    growth = (wealth / nav_start) / (cpi_end / cpi_start)
    return growth ** (DAYS_PER_YEAR / elapsed_days) - 1.0 if growth > 0 else -1.0


# ============================================================================ calendar years
@dataclass(frozen=True)
class YearReturn:
    year: int
    ret: float
    is_partial: bool
    first_week: dt.date
    last_week: dt.date
    start_nav: float
    end_nav: float


def calendar_year_returns(nav_start: float, week_keys, nav_ends) -> list:
    """MET-011/012, Q-040: compounded return per Friday-week_key year; the base of the first
    year is NAV_start, of later years the nav_end of the last retained week of the previous
    year. The first year is partial when the run starts after its first Friday, the last when
    it ends before its last Friday."""
    week_keys, nav_ends = list(week_keys), list(nav_ends)
    if not week_keys:
        return []
    out, base = [], nav_start
    first, last = week_keys[0], week_keys[-1]
    years = sorted({w.year for w in week_keys})
    for y in years:
        idx = [i for i, w in enumerate(week_keys) if w.year == y]
        end = nav_ends[idx[-1]]
        partial = (y == first.year and (first - dt.timedelta(days=7)).year == y) or \
                  (y == last.year and (last + dt.timedelta(days=7)).year == y)
        out.append(YearReturn(y, end / base - 1.0, partial, week_keys[idx[0]], week_keys[idx[-1]],
                              base, end))
        base = end
    return out


def best_and_worst_year(years) -> tuple:
    """(best, worst) YearReturn; ties resolved by the earlier year."""
    if not years:
        return None, None
    best = max(years, key=lambda y: (y.ret, -y.year))
    worst = min(years, key=lambda y: (y.ret, y.year))
    return best, worst


# ============================================================================ trading
def weekly_trades(trades) -> list:
    return [t for t in trades if getattr(t, "phase", "weekly") == "weekly"]


def trade_count(trades) -> int:
    """MET-013, Q-040: weekly economic trades (signal, rebalance, sell_to_pay)."""
    return len(weekly_trades(trades))


def turnover(trades, nav_ends) -> Optional[float]:
    """MET-014, Q-040: sum |gross traded value| of weekly risky-asset trades / mean nav_end."""
    nav_ends = list(nav_ends)
    if not nav_ends:
        return None
    return math.fsum(abs(t.gross_traded_value) for t in weekly_trades(trades)) / mean(nav_ends)


def risk_state_shares(week_states, assets) -> dict:
    """MET-020/021, Q-040: per active risky asset, share of retained weeks with effective state
    RISK_ON / RISK_OFF (no aggregate state for a multi-asset portfolio)."""
    week_states = list(week_states)
    n = len(week_states)
    out = {}
    for a in assets:
        on = sum(1 for s in week_states if getattr(s[a], "value", s[a]) == "RISK_ON")
        off = sum(1 for s in week_states if getattr(s[a], "value", s[a]) == "RISK_OFF")
        out[a] = (on / n, off / n) if n else (None, None)
    return out


# ============================================================================ CPI
@dataclass(frozen=True)
class CpiWindow:
    """REAL-001..004, Q-045: CPI of the inception month and of the last retained week's month."""
    start_month: str
    end_month: str
    cpi_start: Optional[float]
    cpi_end: Optional[float]
    start_imputed: bool
    end_imputed: bool
    label: str
    warning: str
    note: str = ""


def cpi_month_value(cpi, day: dt.date, mapping: str = "previous_available") -> tuple:
    """(value, imputed, note) of the CPI month of ``day``. previous_available: a month missing
    from the file (beyond its end) takes the last earlier available month, flagged; imputed
    months inside the file keep the loader's previous_available flag. month_value: exact
    month or None."""
    ym = (day.year, day.month)
    if ym in cpi.values:
        imputed = ym in cpi.imputed
        return cpi.values[ym], imputed, ("previous_available (empty month in file)" if imputed else "")
    if mapping != "previous_available":
        return None, False, f"CPI month {ym[0]}-{ym[1]:02d} not available (cpi.mapping={mapping})"
    earlier = [k for k in cpi.values if k < ym]
    if not earlier:
        return None, False, f"no CPI on or before {ym[0]}-{ym[1]:02d}"
    k = max(earlier)
    return cpi.values[k], True, f"previous_available: {k[0]}-{k[1]:02d} used for {ym[0]}-{ym[1]:02d}"


def cpi_window(cpi, inception: dt.date, last_week: dt.date, mapping: str, label: str) -> CpiWindow:
    s, s_imp, s_note = cpi_month_value(cpi, inception, mapping)
    e, e_imp, e_note = cpi_month_value(cpi, last_week, mapping)
    warning = US_CPI_WARNING if label == US_CPI_LABEL else f"Real returns deflated by {label}"
    note = "; ".join(x for x in (s_note and f"start: {s_note}", e_note and f"end: {e_note}") if x)
    return CpiWindow(f"{inception.year}-{inception.month:02d}", f"{last_week.year}-{last_week.month:02d}",
                     s, e, s_imp, e_imp, label, warning, note)


# ============================================================================ run metrics
@dataclass(frozen=True)
class PathSeries:
    """Weekly path of one engine run: returns and nav_end per retained week."""
    nav_start: float
    week_keys: tuple
    returns: tuple
    nav_ends: tuple

    @classmethod
    def from_engine(cls, result) -> "PathSeries":
        return cls(result.initial_ledger.nav, tuple(w.week_key for w in result.weeks),
                   tuple(w.portfolio_return for w in result.weeks),
                   tuple(w.nav_end for w in result.weeks))


@dataclass(frozen=True)
class RunMetrics:
    nav_start: float                # = growth_base_nav (CAGR denominator), kept for compatibility
    growth_base_nav: float          # initial capital before the foundation setup cost
    path_start_nav: float           # start of the weekly NAV path (investable capital)
    elapsed_days: int
    weeks: int
    final_wealth_pre_tax: float
    pre_terminal_nav: float
    after_tax_terminal_wealth: float
    cagr: Optional[float]
    after_tax_cagr: Optional[float]
    real_cagr: Optional[float]
    after_tax_real_cagr: Optional[float]
    volatility: Optional[float]
    after_tax_volatility: Optional[float]
    sharpe: Optional[float]
    after_tax_sharpe: Optional[float]
    sortino: Optional[float]
    after_tax_sortino: Optional[float]
    max_drawdown: float
    after_tax_max_drawdown: float
    calmar: Optional[float]
    after_tax_calmar: Optional[float]
    best_year: Optional[YearReturn]
    worst_year: Optional[YearReturn]
    after_tax_best_year: Optional[YearReturn]
    after_tax_worst_year: Optional[YearReturn]
    calendar_years: tuple
    after_tax_calendar_years: tuple
    trade_count: int
    turnover: Optional[float]
    turnover_annualized: Optional[float]
    terminal_trade_count: int
    terminal_traded_value: float
    risk_state_shares: dict
    cpi: Optional[CpiWindow]

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def compute_run_metrics(*, pre: PathSeries, after: PathSeries, elapsed_days: int, rf_returns,
                        rf_after_tax_rate: float, pre_terminal_nav: float,
                        after_tax_terminal_wealth: float, trades, terminal_trades=(),
                        week_states=(), assets=(), mar_annual: float = 0.0,
                        cpi: Optional[CpiWindow] = None, growth_base_nav: Optional[float] = None,
                        final_wealth_pre_tax: Optional[float] = None) -> RunMetrics:
    """Pre-tax metrics on the shadow path (Q-015), after-tax risk metrics on the actual weekly
    path, after-tax CAGR on after-tax terminal wealth; trading metrics on the actual weekly
    trades (MET-001..021, MET-025, MET-026)."""
    if pre.nav_start != after.nav_start:
        raise ValueError("pre-tax and actual runs must start from the same NAV")
    path_start = pre.nav_start
    growth_base = path_start if growth_base_nav is None else growth_base_nav
    rf = list(rf_returns)
    final_pre = pre.nav_ends[-1] if final_wealth_pre_tax is None else final_wealth_pre_tax
    g = cagr(final_pre, growth_base, elapsed_days)
    ga = cagr(after_tax_terminal_wealth, growth_base, elapsed_days)
    dd = max_drawdown(nav_path(path_start, pre.nav_ends))
    dda = max_drawdown(nav_path(path_start, after.nav_ends))
    years = calendar_year_returns(path_start, pre.week_keys, pre.nav_ends)
    years_a = calendar_year_returns(path_start, after.week_keys, after.nav_ends)
    best, worst = best_and_worst_year(years)
    best_a, worst_a = best_and_worst_year(years_a)
    to = turnover(trades, after.nav_ends)
    real = real_after = None
    if cpi is not None and cpi.cpi_start and cpi.cpi_end:
        real = real_cagr(final_pre, growth_base, cpi.cpi_start, cpi.cpi_end, elapsed_days)
        real_after = real_cagr(after_tax_terminal_wealth, growth_base, cpi.cpi_start, cpi.cpi_end,
                               elapsed_days)
    return RunMetrics(
        nav_start=growth_base, growth_base_nav=growth_base, path_start_nav=path_start,
        elapsed_days=elapsed_days, weeks=len(after.week_keys),
        final_wealth_pre_tax=final_pre, pre_terminal_nav=pre_terminal_nav,
        after_tax_terminal_wealth=after_tax_terminal_wealth,
        cagr=g, after_tax_cagr=ga, real_cagr=real, after_tax_real_cagr=real_after,
        volatility=annualized_volatility(pre.returns),
        after_tax_volatility=annualized_volatility(after.returns),
        sharpe=sharpe(pre.returns, rf),
        after_tax_sharpe=sharpe(after.returns, rf_after_tax(rf, rf_after_tax_rate)),
        sortino=sortino(pre.returns, mar_annual), after_tax_sortino=sortino(after.returns, mar_annual),
        max_drawdown=dd, after_tax_max_drawdown=dda, calmar=calmar(g, dd), after_tax_calmar=calmar(ga, dda),
        best_year=best, worst_year=worst, after_tax_best_year=best_a, after_tax_worst_year=worst_a,
        calendar_years=tuple(years), after_tax_calendar_years=tuple(years_a),
        trade_count=trade_count(trades), turnover=to,
        turnover_annualized=None if to is None else to * DAYS_PER_YEAR / elapsed_days,
        terminal_trade_count=len(terminal_trades),
        terminal_traded_value=math.fsum(t.gross_traded_value for t in terminal_trades),
        risk_state_shares=risk_state_shares(week_states, assets), cpi=cpi)
