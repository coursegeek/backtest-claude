"""RISK-004 on initial sleeve construction for random targets, states and fractions."""
import random

import pytest

from src.allocation import initial_sleeves, scale_sleeve
from src.models import SignalParams, State


@pytest.mark.parametrize("seed", range(50))
def test_identity_initial_allocation(seed):
    rng = random.Random(seed)
    raw = [rng.random() for _ in range(4)]
    targets = dict(zip(("stocks", "gold", "btc", "rf"), [x / sum(raw) for x in raw]))
    states = {a: rng.choice([State.RISK_ON, State.RISK_OFF]) for a in ("stocks", "gold", "btc")}
    params = {a: SignalParams(a, 50, 0, 0, 1, 1, 1, rng.random(), "sell_fraction_current")
              for a in states}
    capital = rng.uniform(1e3, 1e9)
    ps = initial_sleeves(capital, targets, states, params)
    ps.check_identity(capital)
    for a, w in ps.weights().items():
        assert abs(w - targets[a]) < 1e-12
    for s in ps.risky:
        t = scale_sleeve(s, s.total * rng.uniform(0.1, 3))
        if s.total:
            assert abs(t.asset_value / t.total - s.asset_value / s.total) < 1e-12
