"""Property tests of the core portfolio engine on random multi-asset synthetic data."""
import math
import random

import pytest

from fixtures.builders import engine_inputs, params_for, random_walk
from src.costs import CostModel
from src.engine import EngineInputs, WeekMarket, run_engine
from src.ledger import COMPONENTS
from src.rf import reserve_name

ASSETS = ("stocks", "gold", "btc")
EPS = 4 * 2.220446049250313e-16
SEEDS = range(12)


def scenario(seed, costs=None, n=90, first=20):
    rng = random.Random(seed)
    hist = {a: random_walk(n, seed * 10 + i, vol=0.04) for i, a in enumerate(ASSETS)}
    rets = {a: [rng.gauss(0.001, 0.03) for _ in range(n - first)] for a in ASSETS}
    raw = [rng.random() + 0.05 for _ in range(4)]
    targets = dict(zip(ASSETS + ("rf",), [x / sum(raw) for x in raw]))
    params = {a: params_for(a, ma=rng.randint(3, 8), confirm_off=rng.randint(1, 3),
                            confirm_on=rng.randint(1, 3), delay=rng.randint(1, 3),
                            threshold_off=rng.choice([0.0, 0.01, 0.03]),
                            threshold_on=rng.choice([0.0, 0.02]),
                            sell_fraction=rng.choice([0.25, 0.5, 1.0]))
              for a in ASSETS}
    if costs is None:
        costs = CostModel(rng.choice([0.0, 5.0, 25.0]), rng.choice([0.0, 3.0]))
    rf = [rng.uniform(-0.0002, 0.002) for _ in range(n - first)]
    return engine_inputs(hist, first, targets, returns=rets, rf=rf, params=params, costs=costs)


@pytest.mark.parametrize("seed", SEEDS)
def test_identity_every_step(seed):
    """RISK-004 / Q-049 after every pipeline step, recomputed independently."""
    res = run_engine(scenario(seed))
    assert any(w.trades for w in res.weeks)
    for w in res.weeks:
        assert [s for s, _ in w.step_ledgers] == [1, 2, 3, 4, 5, 6]
        for _, led in w.step_ledgers:
            comps = [getattr(led, c) for c in COMPONENTS]
            assert min(comps) >= 0
            parts = sum(led.sleeve(a) for a in ASSETS) + led.rf_base
            assert abs(led.nav - parts) / max(1.0, abs(led.nav)) <= 1e-10
            assert led.nav == math.fsum(comps)


@pytest.mark.parametrize("seed", SEEDS)
def test_zero_cost_trades_conserve_nav(seed):
    """With 0 bps a signal trade by itself does not change NAV."""
    res = run_engine(scenario(seed, costs=CostModel()))
    for w in res.weeks:
        assert abs(w.ledger_after_trades.nav - w.nav_start) <= EPS * w.nav_start


@pytest.mark.parametrize("seed", SEEDS)
def test_signal_trade_touches_only_its_sleeve(seed):
    """RISK-003: a signal trade changes only its asset and that asset's reserve."""
    res = run_engine(scenario(seed))
    for w in res.weeks:
        allowed = set()
        for t in w.trades:
            assert t.cash_component == reserve_name(t.asset)
            allowed |= {t.asset, t.cash_component}
        for c in COMPONENTS:
            if c not in allowed:
                assert getattr(w.ledger_after_trades, c) == getattr(w.ledger_start, c)


@pytest.mark.parametrize("seed", SEEDS)
def test_future_does_not_change_past(seed):
    """META-003: changing every return and price after run week c leaves NAV, trades and
    signal records of weeks < c unchanged."""
    base = scenario(seed)
    c = 15 + seed * 4
    cut = base.weeks[c]
    rng = random.Random(1000 + seed)
    market = {w: (m if w < cut else WeekMarket(w, {a: rng.gauss(0, 0.2) for a in ASSETS},
                                               rng.uniform(0, 0.01)))
              for w, m in base.market.items()}
    series = {a: s.replace_points([p if p.week_key < cut else
                                   p.__class__(p.week_key, p.price * rng.uniform(0.3, 3.0),
                                               p.available_at, p.source_date) for p in s.points])
              for a, s in base.signal_series.items()}
    other = EngineInputs(base.weeks, market, series, base.params, base.targets,
                         base.initial_capital, base.costs, trace=True)
    a, b = run_engine(base), run_engine(other)
    assert a.weeks[:c] == b.weeks[:c]
    assert [r for r in a.signal_records if r.week_key < cut] == \
        [r for r in b.signal_records if r.week_key < cut]


@pytest.mark.parametrize("seed", SEEDS)
def test_asset_key_order_does_not_matter(seed):
    base = scenario(seed)
    rev = lambda d: dict(reversed(list(d.items())))
    market = {w: WeekMarket(w, rev(m.asset_returns), m.rf_return) for w, m in base.market.items()}
    other = EngineInputs(base.weeks, market, rev(base.signal_series), rev(base.params),
                         rev(base.targets), base.initial_capital, base.costs, trace=True)
    a, b = run_engine(base), run_engine(other)
    assert a.weeks == b.weeks and a.trades == b.trades and a.signal_records == b.signal_records


@pytest.mark.parametrize("seed", SEEDS)
def test_buy_from_reserve_never_negative(seed):
    """RISK-005 / Q-017: purchase + costs consume the reserve exactly; no negative cash."""
    res = run_engine(scenario(seed, costs=CostModel(30.0, 20.0)))
    buys = [t for t in res.trades if t.side == "buy"]
    assert buys
    for t in buys:
        assert t.reserve_after == 0.0 and -t.net_cash_flow == t.reserve_before
        spent = t.gross_traded_value + t.transaction_cost + t.slippage
        assert spent <= t.reserve_before * (1 + 1e-12) and abs(spent - t.reserve_before) <= 1e-9 * t.reserve_before


@pytest.mark.parametrize("seed", SEEDS)
def test_zero_bps_transfer_is_conservative(seed):
    """0 bps: asset <-> reserve transfers conserve the sleeve value to rounding precision."""
    res = run_engine(scenario(seed, costs=CostModel()))
    assert res.trades
    for t in res.trades:
        before = t.asset_value_before + t.reserve_before
        after = t.asset_value_after + t.reserve_after
        assert abs(after - before) <= EPS * before
        assert t.transaction_cost == 0.0 and t.slippage == 0.0
