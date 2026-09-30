"""Pre-tax shadow run properties (Q-015): the shadow uses the identical prepared EngineInputs
of the actual run, which neither run modifies; only the taxation policy differs."""
import pickle

import pytest

from src.app import build_hooks, build_run, run_pre_tax, find_tax_hooks
from src.config import ResolvedConfig
from src.engine import run_engine


def cfg(profile, mode):
    extra = {"rebalance_band_pp": 1} if mode == "band" else {}
    return ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
        "run": {"start": "2018-01-01", "end": "2021-12-31", "as_of_date": "2026-09-29"},
        "tax": {"profile": profile},
        "portfolio": {"rebalance": mode, "transaction_cost_bps": 10.0, "slippage_bps": 5.0, **extra}})


@pytest.mark.parametrize("mode", ["signal-only", "monthly", "band"])
@pytest.mark.parametrize("profile", ["none", "individual_pl", "family_foundation_15"])
def test_shadow_inputs_identical_and_unchanged(profile, mode):
    c = cfg(profile, mode)
    inputs, ctx = build_run(c)
    frozen = pickle.dumps(inputs)
    markets = {w: id(m) for w, m in inputs.market.items()}
    from src.calendar import inception_date
    hooks = build_hooks(c, inception_date(ctx["first_week"]))
    actual = run_engine(inputs, hooks)
    pre = run_pre_tax(c, inputs, actual, find_tax_hooks(hooks))
    assert pre.inputs is inputs and pickle.dumps(inputs) == frozen
    assert {w: id(m) for w, m in inputs.market.items()} == markets
    assert [w.week_key for w in pre.engine.weeks] == [w.week_key for w in actual.weeks] == list(inputs.weeks)
    assert all(a.market is b.market for a, b in zip(pre.engine.weeks, actual.weeks))
    assert pre.engine.initial_ledger == actual.initial_ledger
    if profile == "none":
        assert pre.engine is actual
    else:
        assert pre.method == "shadow_zero_tax"
        # only costs are paid in a shadow (none for individual_pl; foundation admin costs)
        assert {p.event_type for p in pre.engine.payments} <= {"foundation_annual_admin_cost"}
        # signals do not depend on taxes: identical signal records
        assert pre.engine.signal_records == actual.signal_records
        costs = {t.transaction_cost / t.gross_traded_value for t in pre.engine.trades}
        assert all(abs(x - 0.001) < 1e-15 for x in costs)
