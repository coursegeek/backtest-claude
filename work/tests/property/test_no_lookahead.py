"""META-003 property: changing any data after week T never changes a signal record up to T,
nor the reconstructed state before a start week."""
import datetime as dt

import pytest

from fixtures.builders import price_series, random_walk
from src.models import SignalParams
from src.signal_analysis import evaluate, reconstruct

PARAMS = [SignalParams("stocks", 10, 0.0, 0.0, 2, 2, 1, 0.5, "sell_fraction_current"),
          SignalParams("btc", 8, 0.03, 0.02, 1, 3, 3, 0.5, "sell_fraction_current")]


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("p", PARAMS, ids=lambda p: p.asset)
def test_future_perturbation_does_not_change_past(seed, p):
    prices = random_walk(160, seed)
    base = price_series(prices, avail_days=2 if p.asset == "btc" else 0)
    cut = 60 + seed * 9
    shocked = prices[:cut] + [x * (0.5 if i % 2 else 1.7) for i, x in enumerate(prices[cut:])]
    other = price_series(shocked, avail_days=2 if p.asset == "btc" else 0)
    a, _, _ = evaluate(base, p)
    b, _, _ = evaluate(other, p)
    assert a[:cut] == b[:cut]
    first = base.keys()[cut]
    assert reconstruct(base, p, first) == reconstruct(other, p, first)
