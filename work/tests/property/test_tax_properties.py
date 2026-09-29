"""Property tests of the individual_pl tax integration: accounting identity per week with
taxes, tax totals, annual timing, no negative components, no tax on unrealized gains."""
import math

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk, tax_hooks, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.reporting import weekly_portfolio_rows

ASSETS = ("stocks", "gold", "btc")


@pytest.mark.parametrize("seed", [3, 11, 29])
@pytest.mark.parametrize("mode,band", [("signal-only", None), ("monthly", None), ("band", 2.0)])
def test_taxed_engine_invariants(seed, mode, band):
    n = 180
    hist = {a: random_walk(n, seed=seed + i, vol=0.05) for i, a in enumerate(ASSETS)}
    inp = with_dividends(engine_inputs(hist, first=10,
                                       targets={"stocks": 0.5, "gold": 0.2, "btc": 0.2, "rf": 0.1},
                                       params={a: params_for(a, ma=5, confirm_off=2, confirm_on=2)
                                               for a in ASSETS},
                                       costs=CostModel(10.0, 5.0),
                                       rf=[0.001 * math.sin(i / 7.0) for i in range(n - 10)]), 0.0004)
    hooks, tax = tax_hooks(mode, band, solidarity_threshold_pln=50_000.0)
    res = run_engine(inp, hooks)
    st = tax.state
    rows = weekly_portfolio_rows(res, inp.targets, ASSETS, st.tax_events)
    for w, r in zip(res.weeks, rows):
        for _, led in w.step_ledgers:
            led.check()
        cost = math.fsum(t.transaction_cost + t.slippage for t in w.trades)
        gross = math.fsum(v * (w.market.rf_return if c.startswith("rf") else w.market.asset_returns.get(c, 0.0))
                          for c, v in w.ledger_before_returns.components().items())
        assert w.nav_end == pytest.approx(w.nav_start + gross - cost - r["taxes_paid"], rel=1e-11)
        annual = [e for e in st.tax_events if e.week_key == w.week_key and e.settlement == "annual"]
        if annual:                               # only in the first retained week of a new year
            prev = res.weeks[res.weeks.index(w) - 1].week_key
            assert prev.year < w.week_key.year and all(e.tax_year == prev.year for e in annual)
    total = math.fsum(e.amount for e in st.tax_events)
    assert total == pytest.approx(st.total_tax_paid(), rel=1e-12)
    assert total == pytest.approx(math.fsum(r["taxes_paid"] for r in rows), rel=1e-12)
    # no mark-to-market: the annual base is exactly the sum of that year's realizations
    for y, l in st.annual_liabilities.items():
        assert l.annual_realized == math.fsum(r.realized_gain for r in res.realizations
                                              if r.week_key.year == y)
