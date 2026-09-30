"""TEST-024 (weekly, annual and terminal tax events; summary.csv is not implemented yet and
the final TaxState.total_tax_paid() is the value it will report) and TEST-048."""
import math

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk, tax_hooks, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.reporting import weekly_portfolio_rows
from src.settlement import settle_terminal

ASSETS = ("stocks", "gold", "btc")


def test_tax_events_sum_equals_summary():
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
    # NAV identity: the NAV path falls by exactly the taxes and costs of each week
    for w, r in zip(res.weeks, rows):
        cost = math.fsum(t.transaction_cost + t.slippage for t in w.trades)
        gross = math.fsum(v * (w.market.rf_return if c.startswith("rf") else
                               w.market.asset_returns.get(c, 0.0))
                          for c, v in w.ledger_before_returns.components().items())
        assert w.nav_end == pytest.approx(w.nav_start + gross - cost - r["taxes_paid"], rel=1e-11)


def max_drawdown(path) -> float:
    """Test helper only (the metrics module is not implemented): max peak-to-trough loss."""
    peak, worst = path[0], 0.0
    for v in path:
        peak = max(peak, v)
        worst = max(worst, 1.0 - v / peak)
    return worst


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
    path = [res.weeks[0].nav_start] + [r["nav_end"] for r in rows]
    assert max_drawdown(path) == 0.0                              # rising weekly path
    assert max_drawdown(path + [y]) > 0.0                         # what a terminal "week" would add
    assert [r["portfolio_return"] for r in rows] == [w.portfolio_return for w in weeks_before]
    assert all(r["portfolio_return"] > 0 for r in rows)
