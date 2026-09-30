import datetime as dt

import pytest

from fixtures.builders import engine_inputs, params_for, price_series
from src.costs import CostModel
from src.engine import AmountsDue, PipelineHooks, run_engine
from src.errors import NotImplementedCommand
from src.models import State
from src.signal_analysis import evaluate

EXIT_THEN_REENTRY = [100.0] * 4 + [110.0, 80.0, 70.0, 110.0, 120.0, 125.0]


class Recorder(PipelineHooks):
    def __init__(self):
        self.calls = []

    def investable_capital(self, capital):
        self.calls.append(("0",))
        return capital

    def amounts_due(self, ctx, portfolio):
        self.calls.append(("2", ctx.week))
        return AmountsDue()

    def rebalance_or_fund(self, ctx, portfolio, due):
        self.calls.append(("3", ctx.week))

    def immediate_taxes(self, ctx, portfolio, ledger_before_returns, market):
        assert portfolio.ledger != ledger_before_returns or market.rf_return == 0
        self.calls.append(("5", ctx.week))

    def end_of_week(self, ctx, portfolio):
        self.calls.append(("6", ctx.week))


def test_pipeline_order_trace():
    """PORT-011: steps 0,1..6 run in the fixed order every week; hooks are real extension
    points called with the week."""
    h = Recorder()
    inp = engine_inputs({"stocks": EXIT_THEN_REENTRY}, first=4, targets={"stocks": 0.9, "rf": 0.1},
                        rf=0.001)
    res = run_engine(inp, h)
    assert h.calls[0] == ("0",)
    per_week = [c[0] for c in h.calls[1:]]
    assert per_week == ["2", "3", "5", "6"] * len(inp.weeks)
    assert [s for s, _ in res.weeks[0].step_ledgers] == [1, 2, 3, 4, 5, 6]
    trade_week = next(w for w in res.weeks if w.trades)
    assert all(t.pipeline_step == 1 for t in trade_week.trades)


def test_amounts_due_hook_default_refuses():
    """Step 2/3 extension points: amounts due without sell_to_pay is refused, not ignored."""
    class Due(PipelineHooks):
        def amounts_due(self, ctx, portfolio):
            return AmountsDue(1.0, (("test", 1.0),))
    with pytest.raises(NotImplementedCommand):
        run_engine(engine_inputs({"stocks": EXIT_THEN_REENTRY}, first=4, targets={"stocks": 1.0}), Due())


def test_hooks_can_trade_through_primitives():
    """A future step-3 module trades through WorkingPortfolio primitives in the same loop."""
    from src.models import TradeReason

    class Sell(PipelineHooks):
        def rebalance_or_fund(self, ctx, portfolio, due):
            if ctx.week == inp.weeks[1]:
                portfolio.sell(ctx.week, "stocks", 1000.0, TradeReason.CALENDAR_REBALANCE, "rf_base", step=3)
    inp = engine_inputs({"stocks": [100.0] * 8}, first=4, targets={"stocks": 1.0})
    res = run_engine(inp, Sell())
    assert [(t.reason.value, t.pipeline_step, t.cash_component) for t in res.trades] == [
        ("calendar_rebalance", 3, "rf_base")]
    assert res.weeks[1].ledger_end.rf_base == 1000.0


def test_initial_allocation_is_not_a_trade():
    """Q-017, SIG-018: initialisation creates lots but no trade and no cost."""
    res = run_engine(engine_inputs({"stocks": [100.0] * 8, "gold": [100.0] * 8}, first=4,
                                   targets={"stocks": 0.5, "gold": 0.3, "rf": 0.2},
                                   costs=CostModel(50.0, 50.0)))
    assert res.trades == () and res.initial_ledger.nav == 1_000_000.0
    assert [(l.source, l.cost) for l in res.final_lots["stocks"]] == [("initial", 500_000.0)]


def test_initial_split_from_reconstructed_risk_off():
    """SIG-018: a RISK_OFF history starts the sleeve split 50/50 without a trade."""
    hist = [100.0] * 4 + [70.0, 69.0, 68.0, 67.0]
    res = run_engine(engine_inputs({"stocks": hist}, first=6, targets={"stocks": 1.0}))
    assert res.pre_start["stocks"].effective_state == State.RISK_OFF
    assert (res.initial_ledger.stocks, res.initial_ledger.rf_reserve_stocks) == (500_000.0, 500_000.0)
    assert res.trades == ()


def test_pending_pre_start_execution_is_a_backtest_trade():
    """Q-019: a confirmation before the start with execution week >= start stays pending and
    becomes a normal trade; the initial split follows the effective state."""
    hist = [100.0] * 4 + [70.0, 70.0, 70.0, 70.0, 70.0]
    p = {"stocks": params_for("stocks", delay=3)}
    res = run_engine(engine_inputs({"stocks": hist}, first=5, targets={"stocks": 1.0}, params=p))
    assert res.pre_start["stocks"].effective_state == State.RISK_ON
    assert res.initial_ledger.stocks == 1_000_000.0
    assert [(t.reason.value, t.week_key) for t in res.trades] == [
        ("signal_exit", res.pre_start["stocks"].pending[0].execution_week)]


def test_no_trades_or_taxes_before_start():
    """SIG-019: pre-start history only rebuilds signal state."""
    hist = [100.0, 80.0, 120.0, 70.0, 130.0, 60.0, 140.0, 141.0, 142.0]
    res = run_engine(engine_inputs({"stocks": hist}, first=7, targets={"stocks": 1.0}))
    assert res.pre_start["stocks"].history_weeks == 7
    assert all(t.week_key >= res.weeks[0].week_key for t in res.trades)


def test_reentry_uses_only_own_reserve():
    """RISK-005: re-entry spends the whole dedicated reserve (costs included), nothing else;
    reserve ends at exactly zero."""
    flat = [100.0] * 10
    res = run_engine(engine_inputs({"stocks": EXIT_THEN_REENTRY, "gold": flat}, first=4,
                                   targets={"stocks": 0.5, "gold": 0.3, "rf": 0.2},
                                   costs=CostModel(20.0, 10.0), rf=0.001))
    buy = next(t for t in res.trades if t.reason.value == "signal_reentry")
    w = next(x for x in res.weeks if x.week_key == buy.week_key)
    assert buy.reserve_after == 0.0 and w.ledger_after_signal.rf_reserve_stocks == 0.0
    assert abs(buy.gross_traded_value * 1.003 - buy.reserve_before) < 1e-6
    for c in ("gold", "rf_base", "rf_reserve_gold"):
        assert getattr(w.ledger_after_signal, c) == getattr(w.ledger_start, c)


def test_no_repeat_sell_in_risk_off():
    """RISK-007."""
    hist = [100.0] * 4 + [80.0] + [70.0 - i for i in range(10)]
    res = run_engine(engine_inputs({"stocks": hist}, first=4, targets={"stocks": 1.0}))
    assert [t.reason.value for t in res.trades] == ["signal_exit"]


def test_target_fraction_of_sleeve():
    """RISK-008: target_fraction_of_sleeve sets the asset to (1-sell_fraction) of the sleeve."""
    p = {"stocks": params_for("stocks", sell_fraction=0.3, risk_off_action="target_fraction_of_sleeve")}
    res = run_engine(engine_inputs({"stocks": EXIT_THEN_REENTRY}, first=4, targets={"stocks": 1.0},
                                   params=p))
    t = res.trades[0]
    w = next(x for x in res.weeks if x.week_key == t.week_key)
    assert abs(w.ledger_after_signal.stocks - 0.7 * w.ledger_after_signal.sleeve("stocks")) < 1e-6


def test_rf_components_share_rf_return():
    """RISK-006, PORT-002, PORT-013 (profile none): rf_base and every reserve earn R_rf."""
    hist = [100.0] * 4 + [70.0, 69.0, 68.0, 67.0]
    res = run_engine(engine_inputs({"stocks": hist, "gold": [100.0] * 8}, first=6,
                                   targets={"stocks": 0.6, "gold": 0.2, "rf": 0.2}, rf=0.01))
    w = res.weeks[0]
    for c in ("rf_base", "rf_reserve_stocks", "rf_reserve_gold"):
        assert getattr(w.ledger_end, c) == getattr(w.ledger_before_returns, c) * 1.01


def test_signal_and_return_series_separate():
    """SIG-002: signals come from the signal price, returns from the return series."""
    res = run_engine(engine_inputs({"stocks": [100.0] * 8}, first=4, targets={"stocks": 1.0},
                                   returns={"stocks": [-0.5, -0.5, -0.5, -0.5]}))
    assert res.trades == () and res.weeks[-1].nav_end == 1_000_000.0 * 0.5 ** 4


def test_engine_signals_equal_signal_only_pipeline():
    """The engine uses the same tracker as signal-only analysis: identical records."""
    hist = [100.0 + 10 * ((i * 7) % 5) - i for i in range(40)]
    inp = engine_inputs({"stocks": hist}, first=15, targets={"stocks": 1.0},
                        params={"stocks": params_for("stocks", ma=4, confirm_off=2, delay=2)})
    res = run_engine(inp)
    ref, _, _ = evaluate(price_series(hist, role="stocks"), inp.params["stocks"])
    assert res.signal_records == ref[15:]


def test_lots_track_signal_trades():
    """Cost basis foundation: a partial exit realises gain on FIFO lots, re-entry opens a
    buy lot with basis equal to the cash spent."""
    res = run_engine(engine_inputs({"stocks": EXIT_THEN_REENTRY}, first=4, targets={"stocks": 1.0},
                                   costs=CostModel(10.0, 0.0)))
    sell, buy = res.trades
    assert sell.realized_gain == sell.net_cash_flow - sell.cost_basis
    assert abs(sell.cost_basis - 500_000.0) < 1e-6          # half of the initial lot
    lots = res.final_lots["stocks"]
    assert [l.source for l in lots] == ["initial", "buy"] and lots[1].cost == -buy.net_cash_flow
    assert len(res.realizations) == 1


def test_tax_reduces_nav_when_paid():
    """TAX-005: a tax reduces cash/NAV exactly when it is paid - annual taxes in step 3 of the
    payment week (not in the year they relate to), immediate taxes in step 5 of their week."""
    from fixtures.builders import annual_tax_inputs, tax_hooks
    import datetime as dt
    inp = annual_tax_inputs(costs_bps=(0.0, 0.0))
    hooks, tax = tax_hooks("signal-only", rf_interest_rate=0.0)
    res = run_engine(inp, hooks)
    for w in res.weeks:
        step = dict(w.step_ledgers)
        paid = sum(p.amount for p in w.payments)
        assert abs(step[2].nav - step[3].nav - paid) < 1e-7          # no costs in this fixture
        assert step[2].nav == w.nav_after_signal                        # nothing in step 2
        assert (paid > 0) == (w.week_key in (dt.date(2001, 1, 5), dt.date(2002, 1, 4)))


def test_snapshot_clone_is_independent():
    """PortfolioSnapshot: complete, immutable and deep-copy safe; a portfolio restored from it
    can trade (e.g. terminal settlement) without changing the snapshot, the final weekly
    state or any weekly record of the run."""
    import copy
    import pickle
    from fixtures.builders import annual_tax_inputs, tax_hooks
    from src.engine import WorkingPortfolio
    from src.models import TradeReason
    from src.settlement import settle_terminal
    inp = annual_tax_inputs()
    hooks, tax = tax_hooks("signal-only")
    res = run_engine(inp, hooks)
    snap = res.final_snapshot
    frozen = pickle.dumps((snap, res.weeks, res.final_ledger, res.final_lots, res.trades))
    assert copy.deepcopy(snap) == snap and snap.ledger == res.final_ledger
    assert snap.week_key == inp.weeks[-1]
    assert dict(snap.lots) == {a: l for a, l in res.final_lots.items() if l}
    clone = WorkingPortfolio.from_snapshot(snap)
    assert clone.snapshot(snap.week_key) == snap                      # exact round trip
    clone.sell(snap.week_key, "stocks", snap.ledger.stocks * 0.5, TradeReason.TERMINAL_LIQUIDATION,
               "rf_base", step=None, phase="terminal")
    assert clone.ledger != snap.ledger and clone.book.lots("stocks") != snap.lots_of("stocks")
    settle_terminal(snap, tax.params, tax.state)
    assert pickle.dumps((snap, res.weeks, res.final_ledger, res.final_lots, res.trades)) == frozen
