"""TEST-024 in the scope of the existing tax events (summary.csv and terminal settlement are not
implemented in this build; TaxState.total_tax_paid() is the value summary.csv will report)."""
import math

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk, tax_hooks, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.reporting import weekly_portfolio_rows

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
    # NAV identity: the NAV path falls by exactly the taxes and costs of each week
    for w, r in zip(res.weeks, rows):
        cost = math.fsum(t.transaction_cost + t.slippage for t in w.trades)
        gross = math.fsum(v * (w.market.rf_return if c.startswith("rf") else
                               w.market.asset_returns.get(c, 0.0))
                          for c, v in w.ledger_before_returns.components().items())
        assert w.nav_end == pytest.approx(w.nav_start + gross - cost - r["taxes_paid"], rel=1e-11)
