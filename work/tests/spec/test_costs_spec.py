"""TEST-019 (TEST-047 needs sell_to_pay and terminal liquidation, not implemented yet)."""
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
        drop = w.ledger_start.nav - w.ledger_after_trades.nav
        assert abs(drop - (t.transaction_cost + t.slippage)) < 1e-6
    sell, buy = res.trades
    assert abs(sell.net_cash_flow - sell.gross_traded_value * (1 - 0.0015)) < 1e-9
    assert abs(buy.gross_traded_value * 1.0015 + buy.net_cash_flow) < 1e-9
    assert buy.reserve_after == 0.0
