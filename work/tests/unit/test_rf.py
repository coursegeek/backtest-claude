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
