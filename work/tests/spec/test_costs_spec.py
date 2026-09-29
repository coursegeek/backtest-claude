"""TEST-019 and TEST-047 (partial: terminal liquidation is not implemented yet, so TEST-047
stays IN_PROGRESS; signal, rebalance and sell_to_pay trades are covered here)."""
from fixtures.builders import engine_inputs
from src.costs import CostModel
from src.engine import run_engine

HIST = [100.0] * 4 + [110.0, 80.0, 70.0, 110.0, 120.0, 125.0]


def test_transaction_cost_reduces_nav():
    """TEST-019 / REB-003, REB-004: cost = traded_value*bps/10000 and slippage likewise; NAV
    falls by exactly their sum at the trade (zero returns isolate the trade)."""
    zero = [0.0] * 6
    res = run_engine(engine_inputs({"stocks": HIST}, first=4, targets={"stocks": 1.0},
                                   returns={"stocks": zero}, costs=CostModel(10.0, 5.0)))
    assert [t.reason.value for t in res.trades] == ["signal_exit", "signal_reentry"]
    for t in res.trades:
        assert abs(t.transaction_cost - t.gross_traded_value * 10 / 10000) < 1e-9
        assert abs(t.slippage - t.gross_traded_value * 5 / 10000) < 1e-9
        w = next(w for w in res.weeks if w.week_key == t.week_key)
        drop = w.ledger_start.nav - w.ledger_after_signal.nav
        assert abs(drop - (t.transaction_cost + t.slippage)) < 1e-6
    sell, buy = res.trades
    assert abs(sell.net_cash_flow - sell.gross_traded_value * (1 - 0.0015)) < 1e-9
    assert abs(buy.gross_traded_value * 1.0015 + buy.net_cash_flow) < 1e-9
    assert buy.reserve_after == 0.0


def test_all_trade_cost_scope():
    """TEST-047 / REB-011 (partial): every trade with positive traded value - signal exit and
    re-entry, calendar and band rebalances, sell_to_pay - pays transaction_cost_bps and
    slippage_bps; RF transfers and payments are not trades and pay nothing (Q-017).
    Terminal liquidation is not implemented in this build."""
    from src.engine import AmountsDue
    from src.models import TradeReason
    from src.rebalancing import StrategicHooks
    from fixtures.builders import params_for

    class Due(StrategicHooks):
        def amounts_due(self, ctx, portfolio):
            return AmountsDue(400_000.0) if ctx.index == 5 else AmountsDue()

    hist = [100.0] * 4 + [110.0, 80.0, 70.0, 110.0, 120.0, 125.0, 126.0, 127.0, 128.0, 129.0]
    gold = [100.0] * 14
    rets = {"stocks": [0.03, -0.1, -0.05, 0.08, 0.02, 0.01, 0.0, 0.01, 0.0, 0.02],
            "gold": [0.0, 0.02, 0.01, -0.03, 0.0, 0.01, 0.0, -0.02, 0.0, 0.01]}
    seen = set()
    for mode, band in (("signal-only", None), ("monthly", None), ("band", 1.0)):
        inp = engine_inputs({"stocks": hist, "gold": gold}, first=4,
                            targets={"stocks": 0.5, "gold": 0.3, "rf": 0.2}, returns=rets,
                            costs=CostModel(12.0, 8.0), rf=0.0005,
                            params={"stocks": params_for("stocks"),
                                    "gold": params_for("gold", threshold_off=0.5, threshold_on=0.5)})
        res = run_engine(inp, Due(mode, band))
        for t in res.trades:
            assert t.gross_traded_value > 0
            assert abs(t.transaction_cost - t.gross_traded_value * 0.0012) <= 1e-12 * t.gross_traded_value
            assert abs(t.slippage - t.gross_traded_value * 0.0008) <= 1e-12 * t.gross_traded_value
            seen.add(t.reason)
        for w in res.weeks:
            cost = sum(t.transaction_cost + t.slippage for t in w.trades)
            paid = sum(p.amount for p in w.payments)
            assert abs(w.nav_start - w.nav_before_returns - cost - paid) < 1e-6
    assert {TradeReason.SIGNAL_EXIT, TradeReason.SIGNAL_REENTRY, TradeReason.CALENDAR_REBALANCE,
            TradeReason.BAND_REBALANCE, TradeReason.SELL_TO_PAY} <= seen
