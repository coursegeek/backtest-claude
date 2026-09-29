"""TEST-029 (the other rebalancing tests need the rebalancing module)."""
from fixtures.builders import engine_inputs, params_for
from src.engine import run_engine


def test_weight_drift():
    """TEST-029 / REB-002, PORT-005: without a rebalance event actual sleeve weights drift with
    different returns and are never restored to the targets; signal-only makes no trades
    when no signal changes."""
    rising = [100.0 * 1.01 ** i for i in range(12)]
    falling_but_on = [100.0 * 1.001 ** i for i in range(12)]
    res = run_engine(engine_inputs({"stocks": rising, "gold": falling_but_on}, first=4,
                                   targets={"stocks": 0.5, "gold": 0.5},
                                   returns={"stocks": [0.10] * 8, "gold": [-0.05] * 8}))
    assert res.trades == ()
    w = [x.ledger_end.sleeve_weights() for x in res.weeks]
    assert w[0]["stocks"] > 0.5 and all(b["stocks"] > a["stocks"] for a, b in zip(w, w[1:]))
    s, g = 0.5, 0.5
    for x in res.weeks:
        s, g = s * 1.10, g * 0.95
        assert abs(x.ledger_end.sleeve_weights()["stocks"] - s / (s + g)) < 1e-12
