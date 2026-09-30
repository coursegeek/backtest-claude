"""Foundation terminal settlement units (FND-006, FND-007, Q-032)."""
import dataclasses
import datetime as dt

import pytest

from fixtures.builders import engine_inputs, foundation_hooks, params_for, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.settlement import distribution_base, settle_foundation_terminal

QUIET = {a: params_for(a, threshold_off=0.9, threshold_on=0.9) for a in ("stocks", "gold")}


def test_foundation_distribution_bases():
    """FND-006, Q-032: distributed_amount (default) or gain_only = max(0, distributed -
    initial capital before the setup cost)."""
    assert distribution_base("gain_only", 1_200_000.0, 1_000_000.0) == 200_000.0
    assert distribution_base("gain_only", 900_000.0, 1_000_000.0) == 0.0
    assert distribution_base("distributed_amount", 1_200_000.0, 1_000_000.0) == 1_200_000.0
    # full settlement: a portfolio that grows to exactly 1 200 000 cash before tax
    inp = engine_inputs({"stocks": [100.0] * 7}, first=4, targets={"stocks": 0.5, "rf": 0.5},
                        returns={"stocks": [0.0] * 3}, params={"stocks": QUIET["stocks"]},
                        capital=1_240_000.0)
    for base, expected in (("gain_only", 200_000.0), ("distributed_amount", 1_200_000.0)):
        hooks, fh = foundation_hooks(inp, "family_foundation_19", distribution_tax_base=base,
                                     annual_admin_cost_pln=0.0)
        res = run_engine(inp, hooks)
        t = settle_foundation_terminal(res.final_snapshot, fh.params, fh.state, 1_000_000.0,
                                       inp.weeks[0] - dt.timedelta(days=7))
        assert t.distributed_amount == 1_200_000.0
        assert t.distribution_tax_base == expected
        assert t.distribution_tax == pytest.approx(expected * 0.19, rel=1e-15)
        assert t.after_tax_terminal_wealth == pytest.approx(1_200_000.0 - expected * 0.19, rel=1e-15)
    hooks, fh = foundation_hooks(inp, "family_foundation_19", distribution_tax_base="gain_only",
                                 annual_admin_cost_pln=0.0)
    res = run_engine(inp, hooks)
    t = settle_foundation_terminal(res.final_snapshot, fh.params, fh.state, 1_300_000.0,
                                   inp.weeks[0] - dt.timedelta(days=7))
    assert t.distribution_tax_base == 0.0 and t.distribution_tax == 0.0     # below initial capital


def test_foundation_layers_separate():
    """FND-007: the weekly 15% dividend tax and the terminal distribution tax are separate
    layers - both > 0; the distribution base is the cash after costs, and the dividend tax
    already paid is neither refunded nor deducted again."""
    inp = with_dividends(engine_inputs({"stocks": [100.0] * 20}, first=4, targets={"stocks": 1.0},
                                       returns={"stocks": [0.01] * 16}, params={"stocks": QUIET["stocks"]},
                                       costs=CostModel(10.0, 0.0), capital=1_040_000.0), 0.0005)
    hooks, fh = foundation_hooks(inp, "family_foundation_15")
    res = run_engine(inp, hooks)
    div_tax = fh.state.dividend_tax_paid
    t = settle_foundation_terminal(res.final_snapshot, fh.params, fh.state, 1_040_000.0,
                                   inp.weeks[0] - dt.timedelta(days=7))
    assert div_tax > 0 and t.distribution_tax > 0
    assert t.final_tax_state.dividend_tax_paid == div_tax                     # untouched
    assert t.distribution_tax == pytest.approx(0.15 * (t.pre_terminal_nav - t.terminal_trading_costs
                                                       - t.final_admin_cost.amount), rel=1e-12)
    assert t.final_tax_state.total_tax_paid() == pytest.approx(div_tax + t.distribution_tax, rel=1e-15)
