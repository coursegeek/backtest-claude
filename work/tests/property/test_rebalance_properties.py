"""Property tests for strategic rebalancing and sell_to_pay (TAX-006, TAX-007, TEST-053,
TEST-054, REB-008/009, Q-017): the results do not depend on the order of asset keys, relabelling
the assets relabels the trades, zero-cost reallocations conserve NAV, and the accounting
identity holds after every step."""
import dataclasses
import datetime as dt
import itertools
import math
import random

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk
from src.costs import CostModel
from src.engine import AmountsDue, WeekContext, WorkingPortfolio, run_engine
from src.errors import InsolvencyError
from src.ledger import Ledger
from src.models import State, TradeReason
from src.rebalancing import StrategicHooks, execute_rebalance
from src.sell_to_pay import sell_to_pay

ASSETS = ("stocks", "gold", "btc")
WK = dt.date(2024, 3, 8)
PARAMS = {a: params_for(a) for a in ASSETS}


def random_case(rng):
    comps = {a: rng.choice([0.0, rng.uniform(1, 1000)]) for a in ASSETS}
    states = {a: rng.choice([State.RISK_ON, State.RISK_OFF]) for a in ASSETS}
    for a in ASSETS:
        comps[f"rf_reserve_{a}"] = rng.uniform(0, 500) if states[a] == State.RISK_OFF else 0.0
    comps["rf_base"] = rng.choice([0.0, rng.uniform(0, 300)])
    if math.fsum(comps.values()) == 0.0:          # a portfolio always has a positive NAV
        comps["rf_base"] = rng.uniform(1, 300)
    raw = [rng.uniform(0.05, 1) for _ in range(4)]
    t = dict(zip(ASSETS + ("rf",), (x / math.fsum(raw) for x in raw)))
    costs = CostModel(rng.choice([0.0, 5.0, 25.0, 100.0]), rng.choice([0.0, 3.0, 50.0]))
    nav = math.fsum(comps.values())
    due = rng.choice([0.0, rng.uniform(0, 0.5) * nav])
    return comps, states, t, costs, due


def relabel(comps, states, targets, perm):
    """perm: new asset -> old asset."""
    c = {"rf_base": comps["rf_base"]}
    for a in ASSETS:
        c[a] = comps[perm[a]]
        c[f"rf_reserve_{a}"] = comps[f"rf_reserve_{perm[a]}"]
    t = {a: targets[perm[a]] for a in ASSETS}
    t["rf"] = targets["rf"]
    return c, {a: states[perm[a]] for a in ASSETS}, t


def ctx(targets, states, order):
    return WeekContext(WK, None, 1, {k: targets[k] for k in order + ("rf",)},
                       {k: PARAMS[k] for k in order}, {k: states[k] for k in order},
                       {k: states[k] for k in order})


def run_rebalance(comps, states, targets, costs, due, order=ASSETS):
    pf = WorkingPortfolio(Ledger(**comps), costs, "FIFO", WK)
    ev = execute_rebalance(pf, ctx(targets, states, order), AmountsDue(due),
                           TradeReason.CALENDAR_REBALANCE, "monthly", WK, WK)
    return pf, ev


def run_stp(comps, states, targets, costs, due, order=ASSETS):
    pf = WorkingPortfolio(Ledger(**comps), costs, "FIFO", WK)
    sell_to_pay(pf, ctx(targets, states, order), AmountsDue(due))
    return pf


def by_asset(trades):
    return {t.asset: t for t in trades}


@pytest.mark.parametrize("seed", range(40))
def test_rebalance_permutation_invariance(seed):
    """TEST-054 property: dictionary key order gives bit-identical results; relabelling the
    assets relabels the trades exactly; after the payment every sleeve equals its target and
    NAV = NAV0 - due - costs."""
    rng = random.Random(seed)
    comps, states, t, costs, due = random_case(rng)
    nav0 = math.fsum(comps.values())
    try:
        base, ev = run_rebalance(comps, states, t, costs, due)
    except InsolvencyError:
        pytest.skip("insolvent random case")
    led = base.ledger
    led.check()
    paid_costs = math.fsum(x.transaction_cost + x.slippage for x in base.trades)
    assert abs(led.nav - (nav0 - due - paid_costs)) < 1e-9 * nav0
    for s, v in led.sleeve_weights().items():
        assert abs(v - t[s]) < 1e-11, s
    for a in ASSETS:
        if states[a] == State.RISK_OFF and comps[a] + comps[f"rf_reserve_{a}"] > 0:
            split = comps[a] / (comps[a] + comps[f"rf_reserve_{a}"])
            assert abs(led.asset(a) / led.sleeve(a) - split) < 1e-11       # REB-009
        if states[a] == State.RISK_ON:
            assert led.reserve(a) < 1e-9 * nav0
    for order in itertools.permutations(ASSETS):
        pf, _ = run_rebalance(comps, states, t, costs, due, order)
        assert pf.trades == base.trades and pf.ledger == base.ledger and pf.payments == base.payments
    for perm in itertools.permutations(ASSETS):
        p = dict(zip(ASSETS, perm))
        pf, _ = run_rebalance(*relabel(comps, states, t, p), costs, due)
        new = by_asset(pf.trades)
        old = by_asset(base.trades)
        assert set(new) == {a for a in ASSETS if p[a] in old}
        for a, tr in new.items():
            o = old[p[a]]
            assert (tr.side, tr.gross_traded_value, tr.transaction_cost) == (
                o.side, o.gross_traded_value, o.transaction_cost)
        for a in ASSETS:
            assert abs(pf.ledger.asset(a) - led.asset(p[a])) <= 1e-12 * nav0
            assert abs(pf.ledger.reserve(a) - led.reserve(p[a])) <= 1e-12 * nav0


@pytest.mark.parametrize("seed", range(40))
def test_sell_to_pay_permutation_invariance(seed):
    """TEST-053 property: key order gives identical results; relabelling relabels the
    payments/sales; the funding never makes a component negative; the waterfall is exact."""
    rng = random.Random(1000 + seed)
    comps, states, t, costs, due = random_case(rng)
    due = due or rng.uniform(0, 0.9) * math.fsum(comps.values())
    try:
        base = run_stp(comps, states, t, costs, due)
    except InsolvencyError:
        c = costs.tc_rate + costs.slip_rate
        net = comps["rf_base"] + sum(comps[f"rf_reserve_{a}"] for a in ASSETS) + (1 - c) * sum(
            comps[a] for a in ASSETS)
        assert due > net
        return
    base.ledger.check()
    assert abs(math.fsum(p.amount for p in base.payments) - due) < 1e-9 * max(1, due)
    reserves = sum(comps[f"rf_reserve_{a}"] for a in ASSETS)
    if due <= comps["rf_base"]:
        assert base.trades == [] and {p.funding_source for p in base.payments} == {"rf_base"}
    elif due <= comps["rf_base"] + reserves:
        assert base.trades == [] and base.ledger.rf_base == 0.0
    else:
        assert all(base.ledger.reserve(a) == 0.0 for a in ASSETS) and base.trades
        held = {a: comps[a] for a in ASSETS if comps[a] > 0}
        tot = math.fsum(held.values())
        for tr in base.trades:                       # pro rata to market values
            assert tr.reason == TradeReason.SELL_TO_PAY
            assert abs(tr.gross_traded_value / held[tr.asset] - base.trades[0].gross_traded_value /
                       held[base.trades[0].asset]) < 1e-9
    for order in itertools.permutations(ASSETS):
        pf = run_stp(comps, states, t, costs, due, order)
        assert pf.trades == base.trades and pf.payments == base.payments and pf.ledger == base.ledger
    for perm in itertools.permutations(ASSETS):
        p = dict(zip(ASSETS, perm))
        pf = run_stp(*relabel(comps, states, t, p), costs, due)
        new, old = by_asset(pf.trades), by_asset(base.trades)
        assert set(new) == {a for a in ASSETS if p[a] in old}
        for a, tr in new.items():
            assert abs(tr.gross_traded_value - old[p[a]].gross_traded_value) <= 1e-9 * max(1, due)
        for a in ASSETS:
            assert abs(pf.ledger.asset(a) - base.ledger.asset(p[a])) <= 1e-9 * max(1, due)
            assert abs(pf.ledger.reserve(a) - base.ledger.reserve(p[a])) <= 1e-9 * max(1, due)


@pytest.mark.parametrize("seed", range(10))
def test_zero_cost_rebalance_conserves_nav(seed):
    """Q-017: without costs and amounts due a rebalance only reallocates: NAV is conserved."""
    rng = random.Random(500 + seed)
    comps, states, t, _, _ = random_case(rng)
    pf, ev = run_rebalance(comps, states, t, CostModel(), 0.0)
    nav0 = math.fsum(comps.values())
    assert abs(pf.ledger.nav - nav0) < 1e-12 * nav0 and ev.transaction_costs == 0.0


@pytest.mark.parametrize("mode", ["weekly", "monthly", "band"])
def test_engine_rebalance_invariants(mode):
    """Accounting identity after every step, no signal effect on the week's fixed exposure, and
    identical output for any key order of targets/params/histories."""
    n = 60
    hist = {a: random_walk(n, seed=i + 11, vol=0.05) for i, a in enumerate(ASSETS)}
    targets = {"stocks": 0.5, "gold": 0.2, "btc": 0.2, "rf": 0.1}
    prm = {a: params_for(a, ma=5, confirm_off=2, confirm_on=2, delay=1) for a in ASSETS}
    dues = {7: 20_000.0, 19: 5_000.0, 33: 60_000.0}

    class Due(StrategicHooks):
        def amounts_due(self, ctx, portfolio):
            v = dues.get(ctx.index, 0.0)
            return AmountsDue(v)

    def run(order):
        inp = engine_inputs({a: hist[a] for a in order}, first=10,
                            targets={k: targets[k] for k in order + ("rf",)},
                            params={a: prm[a] for a in order}, costs=CostModel(10.0, 5.0), rf=0.0004)
        return run_engine(inp, Due(mode, 2.0 if mode == "band" else None))

    base = run(ASSETS)
    assert base.rebalance_events
    for w in base.weeks:
        for _, led in w.step_ledgers:
            led.check()
        for c, v in w.ledger_before_returns.components().items():   # PORT-012
            r = w.market.rf_return if c.startswith("rf") else w.market.asset_returns.get(c, 0.0)
            assert abs(w.ledger_end.components()[c] - v * (1 + r)) <= 1e-9 * max(1.0, v)
    for order in itertools.permutations(ASSETS):
        other = run(order)
        assert other.trades == base.trades and other.payments == base.payments
        assert other.rebalance_events == base.rebalance_events and other.final_ledger == base.final_ledger
