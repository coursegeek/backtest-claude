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


# ---------------------------------------------------------------------------------------------
# Strategic rebalancing and sell_to_pay (REB-006..009, TAX-006, TAX-007, Q-039, Q-046)
# ---------------------------------------------------------------------------------------------
import dataclasses
import datetime as dt
import math

import pytest

from src.costs import CostModel
from src.engine import AmountsDue, WeekContext, WorkingPortfolio
from src.errors import InsolvencyError
from src.ledger import Ledger
from src.models import State, TradeReason
from src.rebalancing import StrategicHooks, band_breached, calendar_trigger, execute_rebalance
from src.sell_to_pay import sell_to_pay

D = dt.date.fromisoformat
W1 = D("2024-03-08")
TARGETS = {"stocks": 0.5, "gold": 0.2, "btc": 0.2, "rf": 0.1}
ALL_ON = {a: State.RISK_ON for a in ("stocks", "gold", "btc")}


def ctx(targets=TARGETS, states=ALL_ON, week=W1, prev=None):
    params = {a: params_for(a) for a in ("stocks", "gold", "btc")}
    return WeekContext(week, prev, 1, dict(targets), params, dict(states), dict(states))


def portfolio(costs=CostModel(), **components):
    return WorkingPortfolio(Ledger(**components), costs, "FIFO", W1 - dt.timedelta(days=7))


class Journal(WorkingPortfolio):
    """Records the order of every portfolio primitive call."""

    def __init__(self, *a, **k):
        self.events = []
        super().__init__(*a, **k)

    def sell(self, week, asset, gross, reason, cash_component, *a, **k):
        t = super().sell(week, asset, gross, reason, cash_component, *a, **k)
        self.events.append(("sell", asset))
        return t

    def buy(self, week, asset, traded_value, reason, cash_component, *a, **k):
        t = super().buy(week, asset, traded_value, reason, cash_component, *a, **k)
        self.events.append(("buy", asset))
        return t

    def pay(self, week, amount, event_type, source, context, *a, **k):
        p = super().pay(week, amount, event_type, source, context, *a, **k)
        self.events.append(("pay", source))
        return p

    def transfer(self, week, source, destination, amount, reason, *a, **k):
        t = super().transfer(week, source, destination, amount, reason, *a, **k)
        self.events.append(("transfer", source, destination))
        return t


def test_band_rebalance_pp():
    """TEST-030 / REB-007: band_pp=1 means an absolute deviation of 1.00 percentage point of
    the sleeve weight, not 1% of the target. Target stocks 0.60: 0.609 (1.5% relative, 0.9 pp)
    does not trigger, 0.61 (exactly 1 pp, >=) triggers."""
    t = {"stocks": 0.6, "gold": 0.4, "btc": 0.0, "rf": 0.0}
    assert band_breached(Ledger(stocks=60.9, gold=39.1), t, 1.0)[0] is False
    assert band_breached(Ledger(stocks=60.6, gold=39.4), t, 1.0)[0] is False   # 1% relative
    assert band_breached(Ledger(stocks=61.0, gold=39.0), t, 1.0)[0] is True    # 1.00 pp
    assert band_breached(Ledger(stocks=64.0, gold=36.0), t, 5.0)[0] is False
    assert band_breached(Ledger(stocks=65.0, gold=35.0), t, 5.0)[0] is True
    # sleeve totals count, not the risk-on part: a RISK_OFF split does not move the weight
    assert band_breached(Ledger(stocks=30.0, rf_reserve_stocks=30.0, gold=40.0), t, 1.0)[0] is False

    # in the engine: detection at the end of T, execution at the start of T+1 (step 3)
    prices = [100.0] * 16
    zero, up = [0.0] * 12, [0.0] * 12
    up[2] = 0.0296           # stocks weight 0.6 -> ~0.6070: +0.7 pp, > 1% relative, no trigger
    up[5] = 0.0150           # -> ~0.6105: >= 1 pp at the end of run week 5
    inp = engine_inputs({"stocks": prices, "gold": prices}, first=4,
                        targets={"stocks": 0.6, "gold": 0.4}, returns={"stocks": up, "gold": zero},
                        params={a: params_for(a, threshold_off=0.5, threshold_on=0.5)
                                for a in ("stocks", "gold")})
    res = run_engine(inp, StrategicHooks("band", 1.0))
    ends = [w.ledger_end.sleeve_weights()["stocks"] for w in res.weeks]
    assert 0.606 < ends[2] < 0.61 and ends[5] >= 0.61
    assert [e.week_key for e in res.rebalance_events] == [inp.weeks[6]]
    ev = res.rebalance_events[0]
    assert (ev.reason, ev.trigger_source_week, ev.nominal_execution_week) == (
        TradeReason.BAND_REBALANCE, inp.weeks[5], inp.weeks[6])
    assert abs(ev.max_deviation - (ends[5] - 0.6)) < 1e-15
    assert abs(res.weeks[6].ledger_before_returns.sleeve_weights()["stocks"] - 0.6) < 1e-12
    assert all(t.pipeline_step == 3 and t.reason == TradeReason.BAND_REBALANCE for t in res.trades)


@pytest.mark.parametrize("mode,prev,week,expected", [
    # month: Monday 2024-01-29 is in January, Friday key 2024-02-02 in February -> February
    ("monthly", "2024-01-26", "2024-02-02", True),
    # Monday 2024-05-27..Sunday 2024-06-02: Friday key 2024-05-31 is still May -> no trigger
    ("monthly", "2024-05-24", "2024-05-31", False),
    ("monthly", "2024-05-31", "2024-06-07", True),
    ("monthly", "2024-02-23", "2024-03-01", True),        # Monday 2024-02-26 in February
    ("quarterly", "2024-03-29", "2024-04-05", True),
    ("quarterly", "2024-04-26", "2024-05-03", False),     # new month, same quarter
    ("quarterly", "2024-06-28", "2024-07-05", True),
    ("annually", "2020-12-25", "2021-01-01", True),       # Monday 2020-12-28 is still 2020
    ("annually", "2025-12-26", "2026-01-02", True),
    ("annually", "2024-12-20", "2024-12-27", False),      # Sunday 2024-12-29, Friday in 2024
    ("yearly", "2024-12-27", "2025-01-03", True),         # alias of annually
    ("monthly", "2024-01-19", "2024-02-09", True),        # retained records around dropped weeks
    ("weekly", "2024-01-19", "2024-01-26", True),
    ("monthly", None, "2024-02-02", False),               # first record = initial allocation
    ("weekly", None, "2024-01-26", False),
    ("signal-only", "2024-01-26", "2024-02-02", False),
    ("band", "2024-01-26", "2024-02-02", False),
])
def test_calendar_rebalance_week_key(mode, prev, week, expected):
    """TEST-045 / REB-006, Q-039: the first week of a month/quarter/year is the first retained
    record whose Friday week_key is in the new period (not week_start, not source_date)."""
    from src.rebalancing import normalize_mode
    assert calendar_trigger(normalize_mode(mode), D(prev) if prev else None, D(week)) is expected


def test_calendar_rebalance_weeks_in_engine():
    """TEST-045 in the engine: monthly/quarterly/annual rebalances happen exactly in the
    first retained week of each new period, at step 3, and restore the targets."""
    n = 70
    rng = [0.01 * ((i * 7) % 5 - 2) for i in range(n)]
    prices = [100.0] * (n + 4)
    inp = engine_inputs({"stocks": prices, "gold": prices}, first=4, first_key="1999-11-05",
                        targets={"stocks": 0.6, "gold": 0.3, "rf": 0.1},
                        returns={"stocks": rng, "gold": [-x for x in rng]}, rf=0.0005,
                        costs=CostModel(10.0, 5.0),
                        params={a: params_for(a, threshold_off=0.5, threshold_on=0.5)
                                for a in ("stocks", "gold")})
    for mode, key in (("monthly", lambda d: (d.year, d.month)),
                      ("quarterly", lambda d: (d.year, (d.month - 1) // 3)),
                      ("annually", lambda d: d.year),
                      ("weekly", lambda d: d)):
        res = run_engine(inp, StrategicHooks(mode))
        expected = [b for a, b in zip(inp.weeks, inp.weeks[1:]) if key(a) != key(b)]
        assert [e.week_key for e in res.rebalance_events] == expected, mode
        for e in res.rebalance_events:
            w = next(x for x in res.weeks if x.week_key == e.week_key)
            assert e.reason == TradeReason.CALENDAR_REBALANCE
            assert e.trigger_source_week == e.nominal_execution_week == e.week_key
            for s, v in w.ledger_before_returns.sleeve_weights().items():
                assert abs(v - inp.targets[s]) < 1e-12, (mode, s)
    assert D("2000-01-07") in [e.week_key for e in run_engine(inp, StrategicHooks("annually")).rebalance_events]


def test_sell_to_pay_funding_order():
    """TEST-053 / TAX-006, Q-046: (A) rf_base, (B) RF reserves pro rata to current values,
    (C) stocks/gold/btc pro rata to market values with gross = N/(1-c), costs, cost basis and
    realisation, reason sell_to_pay; signal state untouched; never negative cash."""
    comps = dict(stocks=600.0, gold=200.0, btc=100.0, rf_base=100.0, rf_reserve_stocks=50.0,
                 rf_reserve_gold=150.0)
    costs = CostModel(10.0, 5.0)
    c = 0.0015

    # (A) only rf_base
    pf = Journal(Ledger(**comps), costs, "FIFO", W1)
    sell_to_pay(pf, ctx(), AmountsDue(60.0))
    assert pf.events == [("pay", "rf_base")] and pf.trades == []
    assert pf.ledger.rf_base == 40.0 and pf.payments[0].context == "sell_to_pay:A_rf_base"

    # (A)+(B): reserves pro rata 50:150
    pf = Journal(Ledger(**comps), costs, "FIFO", W1)
    sell_to_pay(pf, ctx(), AmountsDue(200.0))
    assert pf.events == [("pay", "rf_base"), ("pay", "rf_reserve_stocks"), ("pay", "rf_reserve_gold")]
    assert [p.amount for p in pf.payments] == [100.0, 25.0, 75.0] and pf.trades == []
    assert pf.ledger.rf_reserve_stocks == 25.0 and pf.ledger.rf_reserve_gold == 75.0

    # (A)+(B)+(C): assets pro rata 600:200:100 with gross-up
    pf = Journal(Ledger(**comps), costs, "FIFO", W1)
    nav0 = pf.ledger.nav
    sell_to_pay(pf, ctx(), AmountsDue(400.0, (("synthetic_tax", 400.0),)))
    assert pf.events == [("pay", "rf_base"), ("pay", "rf_reserve_stocks"), ("pay", "rf_reserve_gold"),
                         ("sell", "stocks"), ("sell", "gold"), ("sell", "btc"), ("pay", "rf_base")]
    gross = 100.0 / (1 - c)
    for t, v in zip(pf.trades, (600.0, 200.0, 100.0)):
        assert t.reason == TradeReason.SELL_TO_PAY and t.side == "sell" and t.pipeline_step == 3
        assert abs(t.gross_traded_value - gross * v / 900.0) < 1e-12
        assert abs(t.transaction_cost - t.gross_traded_value * 0.001) < 1e-15
        assert abs(t.slippage - t.gross_traded_value * 0.0005) < 1e-15
        assert t.realized_gain is not None and t.cost_basis > 0
    assert len(pf.realizations) == 3
    assert [p.event_type for p in pf.payments] == ["synthetic_tax"] * 4
    assert abs(math.fsum(p.amount for p in pf.payments) - 400.0) < 1e-9
    assert pf.payments[-1].context == "sell_to_pay:C_risky_assets_pro_rata"
    assert abs(pf.ledger.rf_base) < 1e-9 and pf.ledger.rf_reserve_stocks == 0.0
    costs_paid = math.fsum(t.transaction_cost + t.slippage for t in pf.trades)
    assert abs(nav0 - 400.0 - costs_paid - pf.ledger.nav) < 1e-9
    pf.ledger.check()

    # key order of the context dictionaries does not matter
    rev = {k: TARGETS[k] for k in reversed(list(TARGETS))}
    pf2 = Journal(Ledger(**comps), costs, "FIFO", W1)
    sell_to_pay(pf2, ctx(targets=rev, states={k: ALL_ON[k] for k in reversed(list(ALL_ON))}),
                AmountsDue(400.0, (("synthetic_tax", 400.0),)))
    assert pf2.trades == pf.trades and pf2.payments == pf.payments and pf2.ledger == pf.ledger


def test_sell_to_pay_insolvency():
    """TAX-006 (D): the amount cannot exceed the net liquidation value."""
    comps = dict(stocks=100.0, rf_base=10.0)
    net = 10.0 + 100.0 * (1 - 0.002)
    sell_to_pay(portfolio(CostModel(20.0), **comps), ctx(), AmountsDue(net - 1e-6))
    with pytest.raises(InsolvencyError):
        sell_to_pay(portfolio(CostModel(20.0), **comps), ctx(), AmountsDue(net + 1e-3))
    with pytest.raises(InsolvencyError):
        execute_rebalance(portfolio(CostModel(20.0), **comps), ctx(), AmountsDue(net + 1e-3),
                          TradeReason.CALENDAR_REBALANCE, "monthly", W1, W1)


@pytest.mark.parametrize("bps", [(0.0, 0.0), (10.0, 5.0), (80.0, 40.0)])
def test_rebalance_net_of_amounts_due(bps):
    """TEST-054 / TAX-007, PORT-011 step 3: with a rebalance trigger the amounts due are paid
    from sale proceeds; targets are computed on NAV_after_signal - amounts_due and after the
    trades and the payment every sleeve equals its target on the real final NAV, which equals
    NAV_after_signal - amounts_due - transaction costs - slippage."""
    comps = dict(stocks=700.0, gold=100.0, btc=150.0, rf_base=10.0, rf_reserve_gold=40.0)
    states = {"stocks": State.RISK_ON, "gold": State.RISK_OFF, "btc": State.RISK_ON}
    pf = Journal(Ledger(**comps), CostModel(*bps), "FIFO", W1)
    nav0 = pf.ledger.nav
    ev = execute_rebalance(pf, ctx(states=states), AmountsDue(120.0, (("annual_tax", 120.0),)),
                           TradeReason.CALENDAR_REBALANCE, "monthly", W1, W1)
    led = pf.ledger
    led.check()
    costs = math.fsum(t.transaction_cost + t.slippage for t in pf.trades)
    assert abs(led.nav - (nav0 - 120.0 - costs)) < 1e-9
    assert abs(ev.realized_final_nav - ev.planned_final_nav) < 1e-9
    assert ev.nav_net_for_rebalance == nav0 - 120.0 and ev.amounts_due == 120.0
    for s, v in led.sleeve_weights().items():
        assert abs(v - TARGETS[s]) < 1e-12, s
    # RISK_OFF gold keeps its 100/40 split (REB-009)
    assert abs(led.gold / led.sleeve("gold") - 100.0 / 140.0) < 1e-12
    # order: all sells, then the payment from rf_base, then buys (then free reserve moves)
    kinds = [e[0] for e in pf.events]
    first_pay, last_pay = kinds.index("pay"), len(kinds) - 1 - kinds[::-1].index("pay")
    assert all(k == "sell" or k == "transfer" for k in kinds[:first_pay])
    assert all(k in ("buy", "transfer") for k in kinds[last_pay + 1:])
    assert "sell" in kinds[:first_pay] and "buy" in kinds[last_pay + 1:]
    assert all(p.context == "strategic_rebalance" and p.funding_source == "rf_base"
               for p in pf.payments)
    assert not any(t.reason == TradeReason.SELL_TO_PAY for t in pf.trades)
    if bps != (0.0, 0.0):
        assert costs > 0
        for t in pf.trades:
            assert abs(t.transaction_cost - t.gross_traded_value * bps[0] / 1e4) < 1e-12
