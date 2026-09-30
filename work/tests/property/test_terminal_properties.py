"""Property tests of the terminal settlement (IND-016, IND-017, IND-020, PORT-014): it works on
copies, never touches the weekly path or earlier audit records, liquidates everything,
reconciles costs exactly, uses the same loss-bucket algorithm as the annual close, and is
deterministic and independent of the order of asset keys."""
import dataclasses
import datetime as dt
import itertools
import json
import math
import pickle
import random

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk, tax_hooks, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.settlement import settle_terminal
from src.tax import LossBucket, TaxParams, TaxState, close_tax_year

ASSETS = ("stocks", "gold", "btc")
SEEDS = (90, 96, 108)


def taxed_run(seed, mode="monthly", band=None, order=ASSETS):
    n = 170
    hist = {a: random_walk(n, seed=seed + i, vol=0.05) for i, a in enumerate(ASSETS)}
    targets = {"stocks": 0.5, "gold": 0.2, "btc": 0.2, "rf": 0.1}
    prm = {a: params_for(a, ma=5, confirm_off=2) for a in ASSETS}
    inp = with_dividends(engine_inputs({a: hist[a] for a in order}, first=10,
                                       targets={k: targets[k] for k in order + ("rf",)},
                                       params={a: prm[a] for a in order},
                                       costs=CostModel(10.0, 5.0), rf=0.0006), 0.0003)
    hooks, tax = tax_hooks(mode, band, solidarity_threshold_pln=20_000.0)
    return inp, run_engine(inp, hooks), tax


@pytest.mark.parametrize("seed", SEEDS)
def test_terminal_settlement_never_touches_the_weekly_run(seed):
    """Properties 1, 2 and 7: no WeekRecord, trade, payment, realization or earlier tax event
    changes; the pre-terminal tax state is not mutated (the final state is a separate copy)."""
    inp, res, tax = taxed_run(seed)
    frozen = pickle.dumps((res.weeks, res.trades, res.payments, res.transfers, res.realizations,
                           res.final_snapshot))
    state_before = pickle.dumps(tax.state)
    events_before = list(tax.state.tax_events)
    t = settle_terminal(res.final_snapshot, tax.params, tax.state)
    assert pickle.dumps((res.weeks, res.trades, res.payments, res.transfers, res.realizations,
                         res.final_snapshot)) == frozen
    assert pickle.dumps(tax.state) == state_before
    assert t.final_tax_state is not tax.state and t.final_tax_state.loss_buckets is not tax.state.loss_buckets
    assert t.final_tax_state.tax_events[:len(events_before)] == events_before
    assert pickle.dumps(t.tax_state_before_terminal) == state_before
    assert all(e.phase == "terminal" and e.settlement == "terminal" and e.week_key == inp.weeks[-1]
               for e in t.terminal_tax_events)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("mode,band", [("signal-only", None), ("monthly", None), ("band", 2.0)])
def test_terminal_liquidation_and_cost_reconciliation(seed, mode, band):
    """Properties 3 and 5: all risky assets are 0 afterwards; terminal costs are >= 0 and NAV
    before minus NAV after the liquidation equals costs + slippage; wealth = NAV after the
    liquidation minus the terminal taxes; the cash is never negative."""
    inp, res, tax = taxed_run(seed, mode, band)
    t = settle_terminal(res.final_snapshot, tax.params, tax.state)
    led = t.final_cash_ledger
    assert (led.stocks, led.gold, led.btc) == (0.0, 0.0, 0.0)
    assert all(v >= 0 for v in led.components().values())
    assert t.terminal_transaction_costs >= 0 and t.terminal_slippage >= 0
    assert t.pre_terminal_nav - t.nav_after_liquidation == pytest.approx(t.terminal_trading_costs,
                                                                        rel=1e-9, abs=1e-6)
    assert t.after_tax_terminal_wealth == pytest.approx(t.nav_after_liquidation - t.terminal_tax_total,
                                                        rel=1e-12)
    held = [a for a in ASSETS if res.final_snapshot.ledger.asset(a) > 0]
    assert [x.asset for x in t.liquidation_trades] == held                # canonical order


@pytest.mark.parametrize("seed", SEEDS)
def test_terminal_result_independent_of_asset_key_order(seed):
    """Property 4."""
    out = []
    for order in itertools.permutations(ASSETS):
        _, res, tax = taxed_run(seed, order=order)
        t = settle_terminal(res.final_snapshot, tax.params, tax.state)
        out.append((json.dumps(t.breakout(), sort_keys=True, default=str), t.liquidation_trades,
                    t.terminal_tax_events, t.final_cash_ledger,
                    json.dumps(t.final_tax_state.to_dict(), sort_keys=True)))
    assert all(o == out[0] for o in out[1:])


@pytest.mark.parametrize("seed", range(20))
def test_loss_buckets_identical_in_annual_and_terminal_close(seed):
    """Property 6: close_tax_year is one algorithm; only the settlement label, the pipeline
    step and the phase of its events differ between the annual and the terminal close."""
    rng = random.Random(seed)
    st = TaxState()
    for y in range(2000, 2008):
        if rng.random() < 0.6:
            v = rng.uniform(1_000, 200_000)
            st.loss_buckets.append(LossBucket(y, v, v * rng.choice([1.0, rng.random()])))
    st.realizations[2008] = [(rng.choice(ASSETS), rng.uniform(-50_000, 250_000), dt.date(2008, 6, 6))
                             for _ in range(4)]
    p = TaxParams(loss_offset_fraction=rng.choice([1.0, 0.5, 0.25]),
                  loss_carryforward_years=rng.choice([5, 3]), solidarity_threshold_pln=100_000.0)
    a, b = st.copy(), st.copy()
    la = close_tax_year(a, p, 2008, dt.date(2009, 1, 2), "annual")
    lb = close_tax_year(b, p, 2008, dt.date(2008, 12, 26), "terminal")
    strip = lambda l: dataclasses.replace(l, determined_week=None, settlement="")   # noqa: E731
    assert strip(la) == strip(lb)
    assert (a.loss_buckets, a.expired_losses) == (b.loss_buckets, b.expired_losses)
    for ea, eb in zip(a.tax_events, b.tax_events):
        assert (ea.amount, ea.gross_base, ea.taxable_base, ea.rate) == (eb.amount, eb.gross_base,
                                                                        eb.taxable_base, eb.rate)
        assert (ea.settlement, ea.pipeline_step, ea.phase) == ("annual", 2, "weekly")
        assert (eb.settlement, eb.pipeline_step, eb.phase) == ("terminal", None, "terminal")


@pytest.mark.parametrize("seed", SEEDS)
def test_repeated_terminal_settlement_is_identical(seed):
    """Property 8: settling twice from the same snapshot and state gives identical results."""
    _, res, tax = taxed_run(seed, "band", 2.0)
    t1 = settle_terminal(res.final_snapshot, tax.params, tax.state)
    t2 = settle_terminal(res.final_snapshot, tax.params, tax.state)
    assert pickle.dumps(t1) == pickle.dumps(t2)
    assert math.isfinite(t1.after_tax_terminal_wealth)
