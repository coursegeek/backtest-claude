"""Metrics module and pre-tax shadow run (MET-001/002/005/011..014/020/021, REAL-001/003,
Q-015, Q-032, Q-040, Q-045)."""
import datetime as dt
import math

import pytest

from fixtures.builders import staged_proxy_layer
from src import metrics
from src.app import run_portfolio
from src.config import ResolvedConfig
from src.engine import run_engine
from src.models import State, Trade, TradeReason
from src.rebalancing import hooks_from_config

D = dt.date.fromisoformat
S07 = {"allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
       "run": {"start": "2018-01-01", "end": "2020-12-31", "as_of_date": "2026-09-29"},
       "portfolio": {"rebalance": "band", "rebalance_band_pp": 1, "transaction_cost_bps": 10.0}}


def cfg(profile="none", **extra):
    layer = {k: dict(v) for k, v in S07.items()}
    layer["tax"] = {"profile": profile}
    for k, v in extra.items():
        layer.setdefault(k, {}).update(v)
    return ResolvedConfig("run", cli_layer=layer)


def trade(week, gross, phase="weekly", reason=TradeReason.SIGNAL_EXIT):
    return Trade(week, "stocks", "sell", reason, gross, 0.0, 0.0, gross, 0, 0, 0, 0, "rf_base", 0, 0,
                 0.0, phase=phase)


# ------------------------------------------------------------------------------ pure functions
def test_calendar_year_returns():
    """MET-011/012, Q-040: compounded return per Friday-key year from NAV_start / previous year
    end; first year partial when it starts after its first Friday, last when it ends before
    its last Friday; best/worst keep the partial flag."""
    weeks = [D("2020-06-05"), D("2020-12-25"), D("2021-01-01"), D("2021-12-31"), D("2022-01-07"),
             D("2022-03-04")]
    navs = [110.0, 121.0, 120.0, 96.0, 97.0, 144.0]
    ys = metrics.calendar_year_returns(100.0, weeks, navs)
    assert [(y.year, y.is_partial) for y in ys] == [(2020, True), (2021, False), (2022, True)]
    assert [y.ret for y in ys] == pytest.approx([0.21, 96.0 / 121.0 - 1, 144.0 / 96.0 - 1])
    best, worst = metrics.best_and_worst_year(ys)
    assert (best.year, best.is_partial, worst.year, worst.is_partial) == (2022, True, 2021, False)
    full = metrics.calendar_year_returns(100.0, [D("2021-01-01"), D("2021-12-31")], [101.0, 103.0])
    assert [y.is_partial for y in full] == [False]            # 2021-01-01 is the first Friday


def test_trade_count():
    """MET-013, Q-040: weekly-phase trades only; terminal liquidation reported separately."""
    trades = (trade(D("2020-01-03"), 10.0), trade(D("2020-02-07"), 5.0, reason=TradeReason.SELL_TO_PAY),
              trade(D("2020-03-06"), 7.0, "terminal", TradeReason.TERMINAL_LIQUIDATION))
    assert metrics.trade_count(trades) == 2
    m = metrics.compute_run_metrics(
        pre=metrics.PathSeries(100.0, (D("2020-01-03"),), (0.0,), (100.0,)),
        after=metrics.PathSeries(100.0, (D("2020-01-03"),), (0.0,), (100.0,)), elapsed_days=7,
        rf_returns=[0.0], rf_after_tax_rate=0.0, pre_terminal_nav=100.0,
        after_tax_terminal_wealth=100.0, trades=trades[:2], terminal_trades=trades[2:])
    assert (m.trade_count, m.terminal_trade_count, m.terminal_traded_value) == (2, 1, 7.0)


def test_turnover():
    """MET-014, Q-040: sum |gross traded value| of weekly trades / mean weekly nav_end; RF
    transfers, payments, dividend reinvestments and terminal trades excluded."""
    trades = (trade(D("2020-01-03"), 30.0), trade(D("2020-01-10"), 10.0),
              trade(D("2020-01-17"), 50.0, "terminal", TradeReason.TERMINAL_LIQUIDATION))
    assert metrics.turnover(trades, [100.0, 120.0, 140.0]) == pytest.approx(40.0 / 120.0)
    r = run_portfolio(cfg(), write=False)
    m = r.metrics
    expected = math.fsum(t.gross_traded_value for t in r.engine.trades) / (
        math.fsum(w.nav_end for w in r.engine.weeks) / len(r.engine.weeks))
    assert m.turnover == pytest.approx(expected, rel=1e-12) and m.trade_count == len(r.engine.trades)
    assert m.turnover_annualized == pytest.approx(m.turnover * 365.2425 / m.elapsed_days, rel=1e-12)


def test_time_in_state():
    """MET-020/021, Q-040: per active asset share of retained weeks in effective RISK_ON /
    RISK_OFF; the two shares add up to 1; no aggregate portfolio state."""
    states = [{"stocks": State.RISK_ON, "gold": State.RISK_OFF}] * 3 + \
             [{"stocks": State.RISK_OFF, "gold": State.RISK_OFF}]
    sh = metrics.risk_state_shares(states, ("stocks", "gold"))
    assert sh == {"stocks": (0.75, 0.25), "gold": (0.0, 1.0)}
    r = run_portfolio(cfg(), write=False)
    for a, (on, off) in r.metrics.risk_state_shares.items():
        assert on + off == pytest.approx(1.0, abs=1e-15)
        assert on == sum(1 for w in r.engine.weeks if w.effective_states[a] == State.RISK_ON) / len(r.engine.weeks)
    assert set(r.metrics.risk_state_shares) == {"stocks", "gold", "btc"}


def test_real_cagr():
    """MET-005, REAL-001: real_growth = (wealth / NAV_start) / (CPI_end / CPI_start)."""
    assert metrics.real_cagr(121.0, 100.0, 200.0, 210.0, 730.485) == pytest.approx(
        ((1.21 / 1.05) ** 0.5) - 1, rel=1e-12)
    r = run_portfolio(cfg(), write=False)
    c, m = r.metrics.cpi, r.metrics
    assert (c.start_month, c.end_month) == ("2017-12", "2020-12")
    assert m.real_cagr == pytest.approx(
        ((m.final_wealth_pre_tax / m.nav_start) / (c.cpi_end / c.cpi_start)) ** (365.2425 / m.elapsed_days) - 1,
        rel=1e-12)


def test_real_metrics_na_without_cpi(tmp_path):
    """REAL-003: a missing CPI file never blocks the nominal run; real metrics are empty and a
    warning is reported."""
    import csv
    r = run_portfolio(cfg(data={"cpi_file": str(tmp_path / "none.csv")},
                          report={"output_dir": str(tmp_path), "run_name": "nocpi"}))
    assert r.metrics.real_cagr is None and r.metrics.cagr is not None
    assert any(i.code == "cpi_unavailable" for i in r.report.issues)
    row = next(csv.DictReader((r.output_dir / "summary.csv").open(encoding="utf-8")))
    assert row["real_cagr"] == "" and row["cagr"] != ""


def test_final_wealth_fields():
    """MET-001/002, Q-015, Q-032: pre-tax final wealth = final NAV of the weekly pre-tax path
    (shadow run for individual_pl, never terminal-liquidated); after-tax = terminal wealth
    (individual_pl) or the final weekly NAV (none, no terminal settlement)."""
    none = run_portfolio(cfg(), write=False)
    m = none.metrics
    assert none.terminal is None
    assert m.final_wealth_pre_tax == m.pre_terminal_nav == m.after_tax_terminal_wealth == \
        none.engine.final_ledger.nav
    ind = run_portfolio(cfg("individual_pl"), write=False)
    m = ind.metrics
    assert ind.pre_tax.method == "shadow_zero_tax"
    assert m.final_wealth_pre_tax == ind.pre_tax.engine.final_ledger.nav
    assert m.pre_terminal_nav == ind.engine.final_ledger.nav == ind.terminal.pre_terminal_nav
    assert m.after_tax_terminal_wealth == ind.terminal.after_tax_terminal_wealth
    assert m.after_tax_terminal_wealth < m.pre_terminal_nav < m.final_wealth_pre_tax
    assert m.cagr == pytest.approx((m.final_wealth_pre_tax / m.nav_start) ** (365.2425 / m.elapsed_days) - 1)
    assert m.after_tax_cagr == pytest.approx(
        (m.after_tax_terminal_wealth / m.nav_start) ** (365.2425 / m.elapsed_days) - 1)


# ------------------------------------------------------------------------------ shadow run
def test_none_pre_tax_equals_actual():
    """Q-015, profile none: a shadow run on the same inputs is economically identical to the
    actual run: same weekly path, final wealth, CAGR and risk metrics before and after tax."""
    r = run_portfolio(cfg(), write=False)
    shadow = run_engine(r.inputs, hooks_from_config(cfg()))
    assert [w.nav_end for w in shadow.weeks] == [w.nav_end for w in r.engine.weeks]
    assert [w.portfolio_return for w in shadow.weeks] == [w.portfolio_return for w in r.engine.weeks]
    assert shadow.trades == r.engine.trades
    m = r.metrics
    assert r.pre_tax.method == "actual_run_no_taxes" and r.pre_tax.engine is r.engine
    assert m.final_wealth_pre_tax == m.after_tax_terminal_wealth
    for pre, post in (("cagr", "after_tax_cagr"), ("volatility", "after_tax_volatility"),
                      ("sharpe", "after_tax_sharpe"), ("sortino", "after_tax_sortino"),
                      ("max_drawdown", "after_tax_max_drawdown"), ("calmar", "after_tax_calmar"),
                      ("real_cagr", "after_tax_real_cagr"), ("best_year", "after_tax_best_year"),
                      ("worst_year", "after_tax_worst_year")):
        assert getattr(m, pre) == getattr(m, post), pre


def test_individual_shadow_differs_only_by_taxes():
    """Q-015: the shadow run of individual_pl uses the identical EngineInputs object with zero
    tax rates: no tax event amount, no tax payment, same costs model; it equals a run of the
    same inputs without any tax module."""
    r = run_portfolio(cfg("individual_pl"), write=False)
    assert r.pre_tax.inputs is r.inputs
    none_hooks = hooks_from_config(cfg("individual_pl"))
    untaxed = run_engine(r.inputs, none_hooks)
    assert [w.nav_end for w in r.pre_tax.engine.weeks] == [w.nav_end for w in untaxed.weeks]
    assert not r.pre_tax.engine.payments
    assert r.metrics.after_tax_cagr < r.metrics.cagr


def test_default_sortino_mar():
    """MET-024: default Sortino MAR is 0.0 (annual, decimal) and is what the summary uses."""
    assert ResolvedConfig().get("metrics.sortino_mar_annual") == 0.0
    r = run_portfolio(cfg(), write=False)
    assert r.metrics.sortino == metrics.sortino(metrics.PathSeries.from_engine(r.engine).returns, 0.0)
    r4 = run_portfolio(cfg(metrics={"sortino_mar_annual": 0.04}), write=False)
    assert r4.metrics.sortino < r.metrics.sortino


def test_foundation_shadow_final_cost():
    """Q-015 foundation: the pre-tax shadow keeps setup/admin costs and trading costs, zeroes
    every tax, runs on the identical EngineInputs; the final-year admin cost is paid after the
    weekly shadow path through the TAX-006 waterfall (rf_base first, no full liquidation, no
    distribution tax) and final_wealth_pre_tax is the value after it."""
    r = run_portfolio(cfg("family_foundation_15"), write=False)
    sc = r.pre_tax.terminal_cost
    assert r.pre_tax.method == "shadow_zero_tax" and r.pre_tax.inputs is r.inputs
    shadow = r.pre_tax.engine
    assert shadow.initial_ledger.nav == r.engine.initial_ledger.nav == 960_000.0
    assert sc.admin_cost.amount == r.terminal.final_admin_cost.amount > 0
    assert sc.pre_cost_nav == shadow.final_ledger.nav
    trading = math.fsum(t.transaction_cost + t.slippage for t in sc.trades)
    assert sc.final_wealth == pytest.approx(sc.pre_cost_nav - sc.admin_cost.amount - trading, rel=1e-12)
    assert r.metrics.final_wealth_pre_tax == sc.final_wealth
    assert (sc.final_ledger.stocks, sc.final_ledger.gold) != (0.0, 0.0)     # no full liquidation
    assert all(t.phase == "terminal" for t in sc.trades) and all(p.phase == "terminal" for p in sc.payments)
    assert sc.payments and sc.payments[0].funding_source == "rf_base"
    assert not [e for e in sc.final_state.tax_events if e.category == "tax"]
    assert sc.final_state.setup_cost_paid == 40_000.0
    assert sc.final_state.admin_cost_paid == pytest.approx(r.terminal.final_tax_state.admin_cost_paid, rel=1e-12)
    # the cost is not a weekly record of the shadow path
    assert len(shadow.weeks) == len(r.engine.weeks) and r.metrics.max_drawdown == metrics.max_drawdown(
        metrics.nav_path(960_000.0, [w.nav_end for w in shadow.weeks]))


def test_existing_profiles_unchanged_by_base_split():
    """Q-033 refactor regression: none and individual_pl keep growth base = path start = initial
    capital and the values produced before the refactor (quarterly, 10+5 bps,
    2018-01-01..2026-07-31). Mechanics regression with values frozen on the staged data: the
    superseded staged stock-signal and dividend proxies are pinned explicitly
    (work/config/staged_proxy_data.yaml), independent of the canonical defaults."""
    base = {"allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
            "run": {"start": "2018-01-01", "end": "2026-07-31", "as_of_date": "2026-09-29"},
            "portfolio": {"rebalance": "quarterly", "transaction_cost_bps": 10.0, "slippage_bps": 5.0}}
    expected = {
        "none": dict(final_wealth_pre_tax=4458360.052609325, after_tax_terminal_wealth=4458360.052609325,
                     cagr=0.19016696735861927, after_tax_cagr=0.19016696735861927,
                     max_drawdown=0.2633136071330905, sharpe=0.9449084409359356,
                     real_cagr=0.14884044056549595, turnover=7.049316332467002),
        "individual_pl": dict(final_wealth_pre_tax=4384715.759339228,
                              after_tax_terminal_wealth=3531129.440069668, cagr=0.1901707002318782,
                              after_tax_cagr=0.16020425957356865, max_drawdown=0.2633136071330905,
                              after_tax_max_drawdown=0.3142832146394996, sharpe=0.9422979969067643,
                              after_tax_sharpe=0.8518537321342441, real_cagr=0.14837211644399595,
                              turnover=6.938712158429536)}
    for profile, values in expected.items():
        layer = {k: dict(v) for k, v in base.items()}
        layer["tax"] = {"profile": profile}
        layer["data"] = staged_proxy_layer()["data"]
        m = run_portfolio(ResolvedConfig("run", cli_layer=layer), write=False).metrics
        assert m.growth_base_nav == m.path_start_nav == m.nav_start == 1_000_000.0
        for k, v in values.items():
            assert getattr(m, k) == v, (profile, k)
