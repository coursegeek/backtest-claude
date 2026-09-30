"""TEST-024 (weekly, annual and terminal tax events, checked against summary.csv), TEST-036,
TEST-043 and TEST-048 on the metrics module (src/metrics.py) and the real summary.csv."""
import datetime as dt
import math

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk, tax_hooks, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.reporting import weekly_portfolio_rows
from src.settlement import settle_terminal
from src import metrics

ASSETS = ("stocks", "gold", "btc")


def test_tax_events_sum_equals_summary(tmp_path):
    """TEST-024 / MET-015, TAX-004, Q-035: the sum of tax events with category=tax equals the
    total tax paid (dividend + RF + capital gains + solidarity), per type and in total, and
    equals the weekly taxes_paid path; annual liabilities equal their step-3 payments."""
    n = 200
    hist = {a: random_walk(n, seed=40 + i, vol=0.05) for i, a in enumerate(ASSETS)}
    inp = with_dividends(engine_inputs(hist, first=12, targets={"stocks": 0.5, "gold": 0.2, "btc": 0.2,
                                                                "rf": 0.1},
                                       params={a: params_for(a, ma=6, confirm_off=2, confirm_on=2)
                                               for a in ASSETS},
                                       costs=CostModel(10.0, 5.0), rf=0.0009), 0.0004)
    hooks, tax = tax_hooks("monthly", solidarity_threshold_pln=10_000.0)
    res = run_engine(inp, hooks)
    st = tax.state
    by_type = {}
    for e in st.tax_events:
        assert e.category == "tax"
        by_type.setdefault(e.event_type, []).append(e.amount)
    assert set(by_type) == {"dividend_tax", "rf_interest_tax", "capital_gains_tax", "solidarity_tax"}
    assert math.fsum(by_type["solidarity_tax"]) > 0 and math.fsum(by_type["capital_gains_tax"]) > 0
    total = math.fsum(e.amount for e in st.tax_events if e.category == "tax")
    assert total == pytest.approx(st.total_tax_paid(), rel=1e-12)
    assert math.fsum(by_type["dividend_tax"]) == pytest.approx(st.dividend_tax_paid, rel=1e-12)
    assert math.fsum(by_type["rf_interest_tax"]) == pytest.approx(st.rf_tax_paid, rel=1e-12)
    assert math.fsum(by_type["capital_gains_tax"]) == pytest.approx(st.capital_gains_tax_paid, rel=1e-12)
    assert math.fsum(by_type["solidarity_tax"]) == pytest.approx(st.solidarity_tax_paid, rel=1e-12)
    rows = weekly_portfolio_rows(res, inp.targets, ASSETS, st.tax_events)
    assert math.fsum(r["taxes_paid"] for r in rows) == pytest.approx(total, rel=1e-12)
    for t in ("capital_gains_tax", "solidarity_tax"):
        paid = math.fsum(p.amount for p in res.payments if p.event_type == t)
        assert paid == pytest.approx(math.fsum(by_type[t]), rel=1e-12)
    for y, l in st.annual_liabilities.items():
        if l.capital_gains_tax + l.solidarity_tax > 0:
            assert l.paid_week == l.determined_week
    # terminal settlement: weekly taxes + terminal taxes = final total; weekly path excludes them
    before_events = list(st.tax_events)
    t = settle_terminal(res.final_snapshot, tax.params, st)
    final = t.final_tax_state
    assert st.tax_events == before_events and final.tax_events[:len(before_events)] == before_events
    assert {(e.settlement, e.event_type) for e in t.terminal_tax_events} == {
        ("terminal", "capital_gains_tax"), ("terminal", "solidarity_tax")}
    total_final = math.fsum(e.amount for e in final.tax_events if e.category == "tax")
    assert total_final == pytest.approx(final.total_tax_paid(), rel=1e-12)
    weekly_taxes = math.fsum(r["taxes_paid"] for r in rows)
    assert weekly_taxes == pytest.approx(total, rel=1e-12)                 # no terminal tax in it
    assert weekly_taxes + t.terminal_tax_total == pytest.approx(total_final, rel=1e-12)
    assert t.terminal_tax_total > 0
    # summary.csv of a real individual_pl run: totals per type == tax event sums (incl. terminal)
    import csv
    from src.app import run_portfolio
    from src.config import ResolvedConfig
    cfg = ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
        "run": {"start": "2018-01-01", "end": "2021-12-31", "as_of_date": "2026-09-29"},
        "tax": {"profile": "individual_pl"}, "portfolio": {"rebalance": "quarterly"},
        "report": {"output_dir": str(tmp_path), "run_name": "t024"}})
    out = run_portfolio(cfg).output_dir
    summ = next(csv.DictReader((out / "summary.csv").open(encoding="utf-8")))
    evs = list(csv.DictReader((out / "tax_events.csv").open(encoding="utf-8")))
    by = lambda typ: math.fsum(float(e["amount"]) for e in evs if e["event_type"] == typ)  # noqa: E731
    assert float(summ["total_tax_paid"]) == pytest.approx(
        math.fsum(float(e["amount"]) for e in evs if e["category"] == "tax"), rel=1e-12)
    assert float(summ["dividend_tax_paid"]) == pytest.approx(by("dividend_tax"), rel=1e-12)
    assert float(summ["rf_interest_tax_paid"]) == pytest.approx(by("rf_interest_tax"), rel=1e-12)
    assert float(summ["capital_gains_tax_paid"]) == pytest.approx(by("capital_gains_tax"), rel=1e-12)
    assert float(summ["solidarity_tax_paid"]) == pytest.approx(by("solidarity_tax"), rel=1e-12)
    assert float(summ["terminal_tax_total"]) == pytest.approx(math.fsum(
        float(e["amount"]) for e in evs if e["settlement"] == "terminal"), rel=1e-12)
    weekly = list(csv.DictReader((out / "weekly_portfolio.csv").open(encoding="utf-8")))
    assert math.fsum(float(r["taxes_paid"]) for r in weekly) + float(summ["terminal_tax_total"]) == \
        pytest.approx(float(summ["total_tax_paid"]), rel=1e-12)
    assert float(summ["capital_gains_tax_paid"]) > float(summ["terminal_capital_gains_tax"]) > 0
    assert float(summ["foundation_distribution_tax_paid"]) == 0.0
    # NAV identity: the NAV path falls by exactly the taxes and costs of each week
    for w, r in zip(res.weeks, rows):
        cost = math.fsum(t.transaction_cost + t.slippage for t in w.trades)
        gross = math.fsum(v * (w.market.rf_return if c.startswith("rf") else
                               w.market.asset_returns.get(c, 0.0))
                          for c, v in w.ledger_before_returns.components().items())
        assert w.nav_end == pytest.approx(w.nav_start + gross - cost - r["taxes_paid"], rel=1e-11)


def test_terminal_tax_excluded_from_nav_path():
    """TEST-048 / PORT-014, IND-017, MET-026: with a large unrealized gain the weekly path ends
    at X = pre_terminal_nav; terminal liquidation costs and taxes lower after-tax terminal
    wealth to Y < X, but no weekly record is added and the weekly path (and its drawdown)
    does not contain the terminal drop."""
    rising = [100.0 * 1.02 ** i for i in range(30)]
    inp = engine_inputs({"stocks": rising, "gold": [100.0] * 30}, first=4,
                        targets={"stocks": 0.8, "gold": 0.2},
                        returns={"stocks": [0.02] * 26, "gold": [0.001] * 26},
                        params={a: params_for(a, threshold_off=0.9, threshold_on=0.9)
                                for a in ("stocks", "gold")}, costs=CostModel(10.0, 5.0))
    hooks, tax = tax_hooks("signal-only")
    res = run_engine(inp, hooks)
    weeks_before = list(res.weeks)
    t = settle_terminal(res.final_snapshot, tax.params, tax.state)
    x = res.weeks[-1].nav_end
    y = t.after_tax_terminal_wealth
    rows = weekly_portfolio_rows(res, inp.targets, ("stocks", "gold"), tax.state.tax_events)
    assert list(res.weeks) == weeks_before and len(rows) == len(inp.weeks)
    assert rows[-1]["nav_end"] == x == t.pre_terminal_nav
    assert y < x and y == pytest.approx(x - t.terminal_trading_costs - t.terminal_tax_total, rel=1e-12)
    assert t.terminal_capital_gains_tax > 0 and t.terminal_trading_costs > 0
    path = metrics.nav_path(res.initial_ledger.nav, [r["nav_end"] for r in rows])
    assert metrics.max_drawdown(path) == 0.0                      # rising weekly path
    assert metrics.max_drawdown(path + [y]) > 0.0                 # what a terminal "week" would add
    m = metrics.compute_run_metrics(
        pre=metrics.PathSeries.from_engine(res), after=metrics.PathSeries.from_engine(res),
        elapsed_days=7 * len(res.weeks), rf_returns=[w.market.rf_return for w in res.weeks],
        rf_after_tax_rate=0.19, pre_terminal_nav=x, after_tax_terminal_wealth=y,
        trades=res.trades, terminal_trades=t.liquidation_trades)
    assert m.after_tax_max_drawdown == 0.0 and m.max_drawdown == 0.0      # MET-026
    assert m.after_tax_cagr < m.cagr                                      # terminal tax in CAGR only
    assert m.after_tax_calmar is None                                     # no weekly drawdown
    assert m.terminal_trade_count == len(t.liquidation_trades) and m.trade_count == 0
    assert [r["portfolio_return"] for r in rows] == [w.portfolio_return for w in weeks_before]
    assert all(r["portfolio_return"] > 0 for r in rows)


def test_metric_conventions():
    """TEST-036 / MET-003, MET-004, MET-006..010, MET-025, MET-026: hand-computed fixture.
    Pre-tax weekly returns 2%, -1%, 3%, -2% from NAV_start 100 over 28 days, RF 0.1%/week;
    after-tax path 1.9%, -1.1%, 2.9%, -2.1%, after-tax terminal wealth 99.5 (terminal tax),
    individual RF tax 19%."""
    wk = [dt.date(2021, 1, 8) + dt.timedelta(days=7 * i) for i in range(4)]
    r = [0.02, -0.01, 0.03, -0.02]
    ra = [0.019, -0.011, 0.029, -0.021]
    navs = [102.0, 100.98, 104.0094, 101.929212]               # 100 * 1.02 * 0.99 * 1.03 * 0.98
    navs_a = [101.9, 100.7791, 103.7016939, 101.52395832810]   # 100 * 1.019 * 0.989 * ...
    pre = metrics.PathSeries(100.0, tuple(wk), tuple(r), tuple(navs))
    after = metrics.PathSeries(100.0, tuple(wk), tuple(ra), tuple(navs_a))
    m = metrics.compute_run_metrics(pre=pre, after=after, elapsed_days=28, rf_returns=[0.001] * 4,
                                    rf_after_tax_rate=0.19, pre_terminal_nav=navs_a[-1],
                                    after_tax_terminal_wealth=99.5, trades=())
    std = math.sqrt((0.015 ** 2 + 0.015 ** 2 + 0.025 ** 2 + 0.025 ** 2) / 3)   # sample std, ddof=1
    assert m.volatility == pytest.approx(std * math.sqrt(52), rel=1e-12)
    assert m.after_tax_volatility == pytest.approx(std * math.sqrt(52), rel=1e-9)
    assert m.sharpe == pytest.approx((0.005 - 0.001) / std * math.sqrt(52), rel=1e-12)
    # after-tax Sharpe uses the net RF 0.001 - 0.001 * 0.19 = 0.00081
    assert m.after_tax_sharpe == pytest.approx((0.004 - 0.00081) / std * math.sqrt(52), rel=1e-9)
    # Sortino, MAR 0: downside sqrt((0.01^2 + 0.02^2) / 4) over all four weeks
    assert m.sortino == pytest.approx(0.005 / math.sqrt(0.0005 / 4) * math.sqrt(52), rel=1e-12)
    assert m.after_tax_sortino == pytest.approx(
        0.004 / math.sqrt((0.011 ** 2 + 0.021 ** 2) / 4) * math.sqrt(52), rel=1e-12)
    mw = 1.04 ** (1 / 52) - 1                                   # Sortino with MAR 4% p.a.
    expected = (0.005 - mw) / math.sqrt(((-0.01 - mw) ** 2 + (-0.02 - mw) ** 2) / 4) * math.sqrt(52)
    assert metrics.sortino(r, 0.04) == pytest.approx(expected, rel=1e-12)
    # drawdown: running max includes NAV_start; peak 104.0094 -> 101.929212 = -2%
    assert m.max_drawdown == pytest.approx(0.02, rel=1e-12)
    assert m.after_tax_max_drawdown == pytest.approx(0.021, rel=1e-9)     # terminal 99.5 excluded
    assert metrics.max_drawdown([100.0] + navs_a + [99.5]) == pytest.approx(1 - 99.5 / 103.7016939, rel=1e-9)
    g = 1.01929212 ** (365.2425 / 28) - 1
    ga = 0.995 ** (365.2425 / 28) - 1
    assert m.cagr == pytest.approx(g, rel=1e-12) and m.after_tax_cagr == pytest.approx(ga, rel=1e-12)
    assert m.calmar == pytest.approx(g / 0.02, rel=1e-12)
    assert m.after_tax_calmar == pytest.approx(ga / 0.021, rel=1e-9)      # asymmetric by design
    assert m.final_wealth_pre_tax == 101.929212 and m.after_tax_terminal_wealth == 99.5


def test_real_cagr_label(tmp_path):
    """TEST-043 / REAL-001..004, MET-005, Q-045, through the real summary.csv: default CPIAUCNS
    real CAGR is labelled US-CPI-deflated with the exact warning; CPI_start = inception month,
    CPI_end = month of the last retained Friday; the known empty month 2025-10 is filled by
    previous_available and flagged."""
    import csv
    import json
    from src.app import run_portfolio
    from src.config import ResolvedConfig
    from fixtures.builders import STAGED
    cfg = ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=1.0"},
        "run": {"start": "2018-01-01", "end": "2025-10-31", "as_of_date": "2026-09-29"},
        "report": {"output_dir": str(tmp_path), "run_name": "cpi"}})
    out = run_portfolio(cfg).output_dir
    row = next(csv.DictReader((out / "summary.csv").open(encoding="utf-8")))
    assert row["cpi_label"] == "US CPI-U / CPIAUCNS"
    assert row["real_return_warning"] == "Real returns deflated by US CPI; not Polish CPI"
    assert (row["inception_date"], row["effective_last_week"]) == ("2017-12-29", "2025-10-31")
    assert (row["cpi_start_month"], row["cpi_end_month"]) == ("2017-12", "2025-10")
    cpi = {r["observation_date"][:7]: r["CPIAUCNS"]
           for r in csv.DictReader((STAGED / "CPIAUCNS.csv").open(encoding="utf-8"))}
    assert cpi["2025-10"] == ""                                     # the known gap
    cs, ce = float(cpi["2017-12"]), float(cpi["2025-09"])           # previous_available
    assert (float(row["cpi_start"]), float(row["cpi_end"])) == (cs, ce)
    assert row["cpi_end_imputed"] == "true" and row["cpi_start_imputed"] == "false"
    growth = (float(row["final_wealth_pre_tax"]) / float(row["nav_start"])) / (ce / cs)
    assert float(row["real_cagr"]) == pytest.approx(
        growth ** (365.2425 / int(row["elapsed_days"])) - 1, rel=1e-12)
    assert int(row["elapsed_days"]) == 7 * int(row["weeks"])
    assert "cpi_imputed" in (out / "validation_report.csv").read_text()
    manifest = json.loads((out / "data_manifest.json").read_text())
    assert any(p["role"] == "cpi" for p in manifest["sources"])
