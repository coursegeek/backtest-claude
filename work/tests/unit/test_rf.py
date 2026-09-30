"""RF components (RISK-006, PORT-013)."""
import pytest

from fixtures.builders import engine_inputs, tax_hooks
from src.engine import run_engine


def test_reserves_earn_net_rf():
    """RISK-006, PORT-013: every RF reserve earns the RF return and is taxed like rf_base:
    R_rf_net = R_rf - max(R_rf, 0) * rate for individual_pl, R_rf for tax.profile=none."""
    hist = [100.0] * 4 + [70.0, 69.0, 68.0, 67.0]          # RISK_OFF: stocks reserve exists
    inp = engine_inputs({"stocks": hist, "gold": [100.0] * 4 + [60.0, 59.0, 58.0, 57.0]}, first=6,
                        targets={"stocks": 0.4, "gold": 0.4, "rf": 0.2},
                        returns={"stocks": [0.0, 0.0], "gold": [0.0, 0.0]}, rf=[0.002, 0.001])
    none = run_engine(inp)
    hooks, tax = tax_hooks()
    taxed = run_engine(inp, hooks)
    for w0, w1 in zip(none.weeks, taxed.weeks):
        r = w0.market.rf_return
        for c in ("rf_base", "rf_reserve_stocks", "rf_reserve_gold"):
            assert getattr(w0.ledger_before_returns, c) > 0
            assert getattr(w0.ledger_end, c) == getattr(w0.ledger_before_returns, c) * (1 + r)
            assert getattr(w1.ledger_end, c) == pytest.approx(
                getattr(w1.ledger_before_returns, c) * (1 + r - r * 0.19), rel=1e-15)
    comps = {e.component for e in tax.state.tax_events if e.event_type == "rf_interest_tax"}
    assert comps == {"rf_base", "rf_reserve_stocks", "rf_reserve_gold"}


def test_foundation_rf_rate():
    """FND-013: foundation RF income tax uses tax.foundation.rf_interest_rate (default 0: no
    tax); a positive rate taxes positive RF income per component, negative RF gives no credit."""
    from fixtures.builders import foundation_hooks
    hist = [100.0] * 4 + [70.0, 69.0, 68.0, 67.0]
    inp = engine_inputs({"stocks": hist}, first=6, targets={"stocks": 0.8, "rf": 0.2},
                        returns={"stocks": [0.0, 0.0]}, rf=[0.001, -0.0002], capital=540_000.0)
    hooks, fh = foundation_hooks(inp)
    run_engine(inp, hooks)
    assert not [e for e in fh.state.tax_events if e.event_type == "rf_interest_tax"]
    hooks, fh = foundation_hooks(inp, rf_interest_rate=0.1)
    res = run_engine(inp, hooks)
    rf = [e for e in fh.state.tax_events if e.event_type == "rf_interest_tax"]
    assert [(e.week_key, e.component) for e in rf] == [(res.weeks[0].week_key, "rf_base"),
                                                       (res.weeks[0].week_key, "rf_reserve_stocks")]
    w = res.weeks[0]
    assert rf[0].amount == pytest.approx(w.ledger_before_returns.rf_base * 0.001 * 0.1, rel=1e-12)
    assert res.weeks[1].ledger_end.rf_base == res.weeks[1].ledger_before_returns.rf_base * (1 - 0.0002)
