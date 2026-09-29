"""Strategic rebalancing, sell_to_pay and the dropped-week time axis in the central engine
(REB-001/002/006..009/011, TAX-006/007, PORT-011/012, REP-013, Q-012, Q-017, Q-039, Q-046)."""
import dataclasses
import datetime as dt
import math

import pytest

from fixtures.builders import engine_inputs, params_for
from src.costs import CostModel
from src.engine import AmountsDue, PipelineHooks, WeekContext, WorkingPortfolio, run_engine
from src.errors import ConfigError, InsolvencyError
from src.ledger import Ledger
from src.models import State, TradeReason
from src.rebalancing import (StrategicHooks, execute_rebalance, normalize_mode, plan_rebalance,
                             asset_shares)
from src.reporting import weekly_portfolio_rows
from src.signal_analysis import evaluate
from fixtures.builders import price_series

W = dt.timedelta(days=7)
FLAT = [100.0] * 20
QUIET = {a: params_for(a, threshold_off=0.5, threshold_on=0.5) for a in ("stocks", "gold", "btc")}


class DueHooks(StrategicHooks):
    """Synthetic amounts due (no tax module): run-week index -> amount."""

    def __init__(self, mode, dues, band_pp=None):
        super().__init__(mode, band_pp)
        self.dues = dues

    def amounts_due(self, ctx, portfolio):
        v = self.dues.get(ctx.index, 0.0)
        return AmountsDue(v, (("synthetic_due", v),)) if v else AmountsDue()


def drop(inp, *weeks):
    """Remove weeks from the portfolio time axis (missing.return_policy=drop); the signal
    series keeps its observations there."""
    return dataclasses.replace(inp, weeks=tuple(w for w in inp.weeks if w not in weeks),
                               run_start=inp.weeks[0])


# ------------------------------------------------------------------------------ modes
def test_modes():
    """REB-001: all modes accepted, yearly is an alias; band needs band_pp > 0."""
    for m in ("signal-only", "weekly", "monthly", "quarterly", "annually", "band"):
        assert normalize_mode(m) == m
    assert normalize_mode("yearly") == "annually"
    with pytest.raises(ConfigError):
        normalize_mode("daily")
    with pytest.raises(ConfigError):
        StrategicHooks("band")
    with pytest.raises(ConfigError):
        StrategicHooks("band", 0.0)


def test_signal_only_step3_is_noop():
    """REB-002: signal-only makes no strategic trade in step 3; weights drift."""
    inp = engine_inputs({"stocks": FLAT, "gold": FLAT}, first=4, targets={"stocks": 0.5, "gold": 0.5},
                        returns={"stocks": [0.02] * 16, "gold": [-0.01] * 16}, params=QUIET)
    res = run_engine(inp, StrategicHooks("signal-only"))
    assert res.trades == () and res.rebalance_events == () and res.transfers == ()
    assert all(w.ledger_before_returns == w.ledger_after_signal for w in res.weeks)


# ------------------------------------------------------------------------------ planning
def test_targets_on_sleeve_totals():
    """REB-008, PORT-010: every strategic sleeve total (asset + its reserve) is set to its target;
    strategic RF is rf_base only."""
    led = Ledger(stocks=500.0, rf_reserve_stocks=100.0, gold=50.0, btc=250.0, rf_base=100.0)
    t = {"stocks": 0.4, "gold": 0.3, "btc": 0.2, "rf": 0.1}
    states = {"stocks": State.RISK_OFF, "gold": State.RISK_ON, "btc": State.RISK_ON}
    shares = asset_shares(led, states, QUIET)
    plan = plan_rebalance(led, t, 0.0, CostModel(), shares)
    assert plan.final_nav == 1000.0 and plan.target_rf_base == 100.0
    assert abs(plan.target_asset["stocks"] + plan.target_reserve["stocks"] - 400.0) < 1e-12
    assert plan.target_reserve["gold"] == 0.0 and plan.target_reserve["btc"] == 0.0


def test_preserve_signal_split():
    """REB-009: a RISK_OFF sleeve 30/20 scaled to a target of ~100 becomes ~60/40 with real
    costs; the reserve top-up is a free RF transfer; RISK_ON sleeves are 100% asset; the signal
    state is not changed."""
    costs = CostModel(20.0, 10.0)
    pf = WorkingPortfolio(Ledger(stocks=30.0, rf_reserve_stocks=20.0, gold=150.0), costs, "FIFO",
                          dt.date(2024, 1, 5))
    states = {"stocks": State.RISK_OFF, "gold": State.RISK_ON, "btc": State.RISK_ON}
    ctx = WeekContext(dt.date(2024, 1, 12), None, 1, {"stocks": 0.5, "gold": 0.5, "btc": 0.0, "rf": 0.0},
                      QUIET, dict(states), dict(states))
    ev = execute_rebalance(pf, ctx, AmountsDue(), TradeReason.CALENDAR_REBALANCE, "monthly",
                           ctx.week, ctx.week)
    led = pf.ledger
    assert abs(led.stocks / led.sleeve("stocks") - 0.6) < 1e-12
    assert abs(led.sleeve("stocks") - 0.5 * led.nav) < 1e-12 and led.nav < 200.0
    assert 59.8 < led.stocks < 60.0 and 39.8 < led.rf_reserve_stocks < 40.0
    assert led.rf_base < 1e-12 and led.rf_reserve_gold == 0.0
    assert [(t.asset, t.side) for t in pf.trades] == [("gold", "sell"), ("stocks", "buy")]
    assert [(x.source, x.destination) for x in pf.transfers] == [("rf_base", "rf_reserve_stocks")]
    # the free transfer: total cost equals c * traded value of the two real trades only
    c = 0.003
    traded = math.fsum(t.gross_traded_value for t in pf.trades)
    assert abs((200.0 - led.nav) - c * traded) < 1e-12
    assert abs(ev.transaction_costs + ev.slippage - c * traded) < 1e-12
    assert ctx.effective_states == states


def test_rebalance_costs_on_every_trade():
    """REB-011, Q-017: every rebalance buy/sell of stocks/gold/btc pays cost and slippage on
    its traded value; RF transfers pay nothing."""
    inp = engine_inputs({"stocks": FLAT, "gold": FLAT, "btc": FLAT}, first=4,
                        targets={"stocks": 0.4, "gold": 0.3, "btc": 0.2, "rf": 0.1},
                        returns={"stocks": [0.03] * 16, "gold": [-0.02] * 16, "btc": [0.05] * 16},
                        params=QUIET, costs=CostModel(15.0, 7.0), rf=0.001)
    res = run_engine(inp, StrategicHooks("weekly"))
    assert len(res.rebalance_events) == len(inp.weeks) - 1 and res.trades
    for t in res.trades:
        assert t.reason == TradeReason.CALENDAR_REBALANCE and t.gross_traded_value > 0
        assert abs(t.transaction_cost - t.gross_traded_value * 0.0015) < 1e-12 * max(1, t.gross_traded_value)
        assert abs(t.slippage - t.gross_traded_value * 0.0007) < 1e-12 * max(1, t.gross_traded_value)
    for w in res.weeks[1:]:
        cost = math.fsum(t.transaction_cost + t.slippage for t in w.trades)
        assert abs(w.nav_after_signal - w.nav_before_returns - cost) < 1e-7


def test_signal_trade_step1_and_rebalance_step3_same_week():
    """PORT-011: a signal exit executes at step 1 and the calendar rebalance of the same week at
    step 3 on the post-signal ledger, preserving the fresh RISK_OFF split (REB-009)."""
    # first_key 2000-01-07: run weeks start 2000-02-04 (index 4); exit confirmed at the end of
    # 2000-02-25, executed (delay 1) in 2000-03-03 = first week of March
    hist = [100.0] * 7 + [70.0] + [70.0] * 6
    inp = engine_inputs({"stocks": hist, "gold": [100.0] * 14}, first=4,
                        targets={"stocks": 0.6, "gold": 0.3, "rf": 0.1},
                        returns={"stocks": [0.01] * 10, "gold": [0.0] * 10}, costs=CostModel(10.0, 5.0),
                        params={"stocks": params_for("stocks"), "gold": QUIET["gold"]})
    res = run_engine(inp, StrategicHooks("monthly"))
    w = next(x for x in res.weeks if x.week_key == dt.date(2000, 3, 3))
    assert [(t.reason, t.pipeline_step) for t in w.trades][0] == (TradeReason.SIGNAL_EXIT, 1)
    assert {t.pipeline_step for t in w.trades[1:]} == {3} and len(w.trades) > 1
    assert all(t.reason == TradeReason.CALENDAR_REBALANCE for t in w.trades[1:])
    assert w.rebalance is not None and w.effective_states["stocks"] == State.RISK_OFF
    before, after_signal = w.ledger_before_returns, w.ledger_after_signal
    split = after_signal.stocks / after_signal.sleeve("stocks")      # 0.5 sold net of costs
    assert after_signal.rf_reserve_stocks > 0 and 0.5 < split < 0.501
    assert abs(before.stocks / before.sleeve("stocks") - split) < 1e-12
    for s, v in before.sleeve_weights().items():
        assert abs(v - inp.targets[s]) < 1e-12
    # the rebalance does not touch the signal state machine
    assert [t.reason for t in res.trades if t.reason.value.startswith("signal")] == [TradeReason.SIGNAL_EXIT]


def test_weight_start_from_ledger_before_returns():
    """PORT-012, REP-013: weight_start_* in weekly_portfolio is the state after all
    start-of-week transactions (step 3), and the week's return is earned on exactly it."""
    class Step3(PipelineHooks):
        def rebalance_or_fund(self, ctx, portfolio, due):
            if ctx.index == 2:
                portfolio.sell(ctx.week, "stocks", 250_000.0, TradeReason.CALENDAR_REBALANCE,
                               "rf_base", step=3)

    inp = engine_inputs({"stocks": FLAT, "gold": FLAT}, first=4, targets={"stocks": 0.6, "gold": 0.4},
                        returns={"stocks": [0.1] * 16, "gold": [0.0] * 16}, params=QUIET)
    res = run_engine(inp, Step3())
    w = res.weeks[2]
    assert w.ledger_after_signal.rf_base == 0.0 and w.ledger_before_returns.rf_base == 250_000.0
    rows = weekly_portfolio_rows(res, inp.targets, ("stocks", "gold"))
    for s, v in w.ledger_before_returns.sleeve_weights().items():
        assert rows[2][f"weight_start_{s}"] == v
    assert rows[2]["weight_start_rf"] != w.ledger_after_signal.sleeve_weights()["rf"]
    assert rows[2]["nav_after_signal"] == w.nav_after_signal
    assert rows[2]["nav_before_returns"] == w.nav_before_returns
    assert rows[2]["target_stocks"] == 0.6 and rows[2]["weight_end_stocks"] != 0.6
    gross = w.ledger_end.nav / w.ledger_before_returns.nav - 1.0
    wts = w.ledger_before_returns.sleeve_weights()
    assert abs(gross - (wts["stocks"] * 0.1)) < 1e-12


# ------------------------------------------------------------------------------ sell_to_pay
def test_sell_to_pay_in_engine_keeps_signal_state():
    """TAX-006: synthetic amounts due without a trigger are funded by sell_to_pay; a RISK_OFF
    sleeve stays RISK_OFF and later re-enters normally."""
    hist = [100.0] * 4 + [70.0] + [70.0] * 4 + [150.0] * 6
    inp = engine_inputs({"stocks": hist}, first=4, targets={"stocks": 0.9, "rf": 0.1},
                        returns={"stocks": [0.0] * 11}, costs=CostModel(10.0, 0.0))
    ref = run_engine(inp, StrategicHooks("signal-only"))
    res = run_engine(inp, DueHooks("signal-only", {3: 700_000.0}))
    w = res.weeks[3]
    assert w.effective_states["stocks"] == State.RISK_OFF
    assert [p.context for p in w.payments] == ["sell_to_pay:A_rf_base", "sell_to_pay:B_reserves_pro_rata",
                                              "sell_to_pay:C_risky_assets_pro_rata"]
    assert [t.reason for t in w.trades] == [TradeReason.SELL_TO_PAY]
    # the signal machine is untouched: identical records and states as without the payment
    assert res.signal_records == ref.signal_records
    assert [w.effective_states for w in res.weeks] == [w.effective_states for w in ref.weeks]
    assert res.weeks[-1].effective_states["stocks"] == State.RISK_ON
    # (B) consumed the whole reserve, so the re-entry has no cash to spend (RISK-005: only the
    # dedicated reserve) and produces no trade; the exit trade is the same as in the reference
    assert [(t.reason, t.week_key) for t in ref.trades] == [
        (TradeReason.SIGNAL_EXIT, res.trades[0].week_key), (TradeReason.SIGNAL_REENTRY, dt.date(2000, 3, 17))]
    assert [t.reason for t in res.trades] == [TradeReason.SIGNAL_EXIT, TradeReason.SELL_TO_PAY]


def test_insolvency_in_engine():
    inp = engine_inputs({"stocks": FLAT}, first=4, targets={"stocks": 1.0}, params=QUIET)
    with pytest.raises(InsolvencyError):
        run_engine(inp, DueHooks("signal-only", {2: 2_000_000.0}))
    with pytest.raises(InsolvencyError):
        run_engine(inp, DueHooks("weekly", {2: 2_000_000.0}))


def test_rebalance_week_pays_due_without_sell_to_pay():
    """TAX-006 notes: with a trigger the week uses TAX-007, never sell_to_pay."""
    inp = engine_inputs({"stocks": FLAT, "gold": FLAT}, first=4, targets={"stocks": 0.5, "gold": 0.5},
                        returns={"stocks": [0.02] * 16, "gold": [0.0] * 16}, params=QUIET,
                        costs=CostModel(10.0, 5.0))
    res = run_engine(inp, DueHooks("weekly", {3: 50_000.0}))
    w = res.weeks[3]
    assert w.rebalance is not None and w.rebalance.amounts_due == 50_000.0
    assert {p.context for p in w.payments} == {"strategic_rebalance"}
    assert not any(t.reason == TradeReason.SELL_TO_PAY for t in res.trades)
    for s, v in w.ledger_before_returns.sleeve_weights().items():
        assert abs(v - inp.targets[s]) < 1e-12


# ------------------------------------------------------------------------------ dropped weeks
def test_pending_band_trigger_through_dropped_week():
    """REB-007, Q-012: a band breach at the end of T executes at the start of the next retained
    week; when T+1 was dropped the audit keeps source T, nominal T+1, actual T+2."""
    up = [0.0] * 16
    up[3] = 0.05
    inp = engine_inputs({"stocks": FLAT, "gold": FLAT}, first=4, targets={"stocks": 0.6, "gold": 0.4},
                        returns={"stocks": up, "gold": [0.0] * 16}, params=QUIET)
    t = inp.weeks[3]
    res = run_engine(drop(inp, t + W), StrategicHooks("band", 1.0))
    ev = res.rebalance_events[0]
    assert (ev.trigger_source_week, ev.nominal_execution_week, ev.week_key) == (t, t + W, t + 2 * W)
    assert t + W not in [w.week_key for w in res.weeks]


def test_dropped_week_signal_regression():
    """Q-012: the observation of a dropped week D is not fed to the in-run signal machine (it
    cannot confirm anything used by the engine), while the signal-only analysis still uses it;
    an execution nominally due in D runs in the next retained week with its nominal week."""
    # run weeks from index 4; exit confirmed at the end of index 4 (70 < SMA), executed at
    # index 5 = D; D's price 200 would confirm a re-entry with confirm_on=1
    hist = [100.0] * 4 + [70.0, 200.0, 60.0, 60.0, 60.0]
    inp = engine_inputs({"stocks": hist}, first=4, targets={"stocks": 1.0})
    dropped = inp.weeks[1]
    res = run_engine(drop(inp, dropped))
    assert ("stocks", dropped) in res.skipped_signal_observations
    assert dropped not in [r.week_key for r in res.signal_records]
    assert [(t.reason, t.week_key, t.nominal_execution_week) for t in res.trades] == [
        (TradeReason.SIGNAL_EXIT, inp.weeks[2], dropped)]
    # signal-only analysis (unchanged) does confirm a re-entry in D
    records, _, _ = evaluate(price_series(hist, role="stocks"), inp.params["stocks"])
    rec_d = next(r for r in records if r.week_key == dropped)
    assert rec_d.confirmation and rec_d.confirmed_state == State.RISK_ON
