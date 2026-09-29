"""TEST-007, TEST-008, TEST-013."""
from fixtures.builders import ff_text, write_csv
from src.data_loader import load_ff, load_gold


def test_ff_percent_to_decimal(tmp_path):
    """TEST-007 / PORT-001, PORT-002: R_stock=(Mkt-RF+RF)/100 and R_rf=RF/100."""
    p = tmp_path / "ff.csv"
    p.write_text(ff_text([("19260702", 1.58, -0.61, -0.90, 0.06)]), encoding="utf-8")
    ff = load_ff(p)
    assert abs(ff.stock_total.points[0].value - 0.0164) < 1e-15
    assert abs(ff.rf.points[0].value - 0.0006) < 1e-15


def test_gold_return_price_ratio(tmp_path):
    """TEST-008 / PORT-003, SCHEMA-003: returns are LBMA price ratios; a missing weekly_return
    column is computed from prices."""
    p = write_csv(tmp_path / "gold.csv", ["week_end", "gold_pm_usd"],
                  [["2020-01-03", "100"], ["2020-01-10", "110"], ["2020-01-17", "99"]])
    rets = [r.value for r in load_gold(p).returns()]
    assert [round(x, 15) for x in rets] == [0.1, -0.1]


def test_stock_net_return_after_dividend_tax():
    """TEST-013 / DIV-007, DIV-006, Q-016: with R_total = 0.01, d = 0.0005 and rate 0.19 the stock
    value earns R_total - d + d*(1 - rate) = 0.009905 in the week; the Fama-French total return
    is reduced only by the tax on the supplied dividend (no double deduction); existing units
    move by the price component R_total - d and the net dividend buys new units."""
    import pytest
    from fixtures.builders import engine_inputs, params_for, tax_hooks, with_dividends
    from src.engine import run_engine

    inp = with_dividends(engine_inputs({"stocks": [100.0] * 7}, first=4, targets={"stocks": 1.0},
                                       returns={"stocks": [0.01, 0.01, 0.01]},
                                       params={"stocks": params_for("stocks", threshold_off=0.9)}),
                         0.0005)
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    for w in res.weeks:
        net = w.ledger_end.stocks / w.ledger_before_returns.stocks - 1.0
        assert net == pytest.approx(0.01 - 0.0005 + 0.0005 * (1 - 0.19), abs=1e-15)
        assert net == pytest.approx(0.009905, abs=1e-15)
        assert w.ledger_after_returns.stocks == w.ledger_before_returns.stocks * 1.01   # gross step 4
    prices = [w.dividends[0].unit_price for w in res.weeks]
    assert prices[0] == pytest.approx(1.0095, abs=1e-15)                # 1 * (1 + 0.01 - 0.0005)
    assert prices[1] == pytest.approx(1.0095 ** 2, abs=1e-15)
    # profile none / rate 0: the full total return, no tax
    hooks0, tax0 = tax_hooks(dividend_rate=0.0, rf_interest_rate=0.0)
    res0 = run_engine(inp, hooks0)
    assert res0.weeks[0].ledger_end.stocks == pytest.approx(1_000_000.0 * 1.01, abs=1e-9)
    assert not tax0.state.tax_events
