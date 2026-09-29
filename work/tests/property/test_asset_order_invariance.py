"""Results do not depend on asset/dictionary ordering."""
import itertools

from src.allocation import initial_sleeves
from src.config import ResolvedConfig
from src.models import SignalParams, State

P = {a: SignalParams(a, 50, 0, 0, 1, 1, 1, 0.4, "sell_fraction_current") for a in ("stocks", "gold", "btc")}


def test_initial_sleeves_order_invariant():
    items = [("stocks", 0.35), ("gold", 0.25), ("btc", 0.15), ("rf", 0.25)]
    states = [("stocks", State.RISK_OFF), ("gold", State.RISK_ON), ("btc", State.RISK_OFF)]
    ref = None
    for perm in itertools.permutations(items):
        for sperm in itertools.permutations(states):
            ps = initial_sleeves(1_000_000.0, dict(perm), dict(sperm), P)
            ref = ref or ps
            assert ps == ref


def test_config_key_order_invariant():
    a = ResolvedConfig(file_layer={"signals": {"gold": {"threshold": 0.01, "confirm_weeks": 3},
                                               "btc": {"ma_length": 30}}})
    b = ResolvedConfig(file_layer={"signals": {"btc": {"ma_length": 30},
                                               "gold": {"confirm_weeks": 3, "threshold": 0.01}}})
    assert all(a.signal_params(x) == b.signal_params(x) for x in ("stocks", "gold", "btc"))
    assert a.to_yaml() == b.to_yaml()
