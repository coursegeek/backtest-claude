"""TEST-019 and TEST-047 (signal, rebalance, sell_to_pay, terminal liquidation and foundation
distribution liquidation trades)."""
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
    """TEST-047 / REB-011, REB-003, REB-004, Q-017: every trade of a risky asset with positive
    traded value - signal exit and re-entry, calendar and band rebalances, sell_to_pay and the
    terminal liquidation - pays cost = gross_traded_value * bps / 10000 for transaction costs
    and for slippage alike; RF transfers, payments and dividend reinvestments are not trades
    and pay nothing. No transaction type is exempt."""
    import datetime as dt
    import math
    import pytest
    from src.engine import AmountsDue, ComposedHooks, PipelineHooks
    from src.models import TradeReason
    from src.rebalancing import StrategicHooks
    from src.settlement import settle_terminal
    from src.tax import IndividualTaxHooks, TaxParams
    from fixtures.builders import params_for

    class SyntheticDue(PipelineHooks):                  # forces sell_to_pay without a trigger
        def amounts_due(self, ctx, portfolio):
            return AmountsDue(400_000.0, (("synthetic_due", 400_000.0),)) if ctx.index == 5 \
                else AmountsDue()

    hist = [100.0] * 4 + [110.0, 80.0, 70.0, 110.0, 120.0, 125.0, 126.0, 127.0, 128.0, 129.0]
    gold = [100.0] * 14
    rets = {"stocks": [0.03, -0.1, -0.05, 0.08, 0.02, 0.01, 0.0, 0.01, 0.0, 0.02],
            "gold": [0.0, 0.02, 0.01, -0.03, 0.0, 0.01, 0.0, -0.02, 0.0, 0.01]}
    seen, tc_bps, slip_bps = set(), 12.0, 8.0
    for mode, band in (("signal-only", None), ("monthly", None), ("band", 1.0)):
        inp = engine_inputs({"stocks": hist, "gold": gold}, first=4,
                            targets={"stocks": 0.5, "gold": 0.3, "rf": 0.2}, returns=rets,
                            costs=CostModel(tc_bps, slip_bps), rf=0.0005,
                            params={"stocks": params_for("stocks"),
                                    "gold": params_for("gold", threshold_off=0.5, threshold_on=0.5)})
        tax = IndividualTaxHooks(TaxParams())
        res = run_engine(inp, ComposedHooks(StrategicHooks(mode, band), tax, SyntheticDue()))
        term = settle_terminal(res.final_snapshot, tax.params, tax.state)
        assert term.liquidation_trades and all(t.phase == "terminal" for t in term.liquidation_trades)
        for t in res.trades + term.liquidation_trades:
            assert t.gross_traded_value > 0
            assert t.transaction_cost == pytest.approx(t.gross_traded_value * tc_bps / 10000, rel=1e-12)
            assert t.slippage == pytest.approx(t.gross_traded_value * slip_bps / 10000, rel=1e-12)
            if t.side == "sell":
                assert t.net_cash_flow == pytest.approx(
                    t.gross_traded_value - t.transaction_cost - t.slippage, rel=1e-12)
            seen.add(t.reason)
        for w in res.weeks:
            cost = sum(t.transaction_cost + t.slippage for t in w.trades)
            paid = sum(p.amount for p in w.payments)
            assert abs(w.nav_start - w.nav_before_returns - cost - paid) < 1e-6
        assert term.pre_terminal_nav - term.nav_after_liquidation == pytest.approx(
            term.terminal_transaction_costs + term.terminal_slippage, rel=1e-9)
        assert term.terminal_transaction_costs == pytest.approx(
            math.fsum(t.gross_traded_value for t in term.liquidation_trades) * tc_bps / 10000, rel=1e-12)
        assert all(x.amount > 0 for x in term.terminal_transfers)     # free: NAV unchanged
        # foundation terminal settlement (REB-011: foundation_distribution_liquidation)
        from fixtures.builders import foundation_hooks
        from src.settlement import settle_foundation_terminal
        fhooks, fh = foundation_hooks(inp, mode=mode, band_pp=band)
        fres = run_engine(inp, fhooks)
        fterm = settle_foundation_terminal(fres.final_snapshot, fh.params, fh.state, 1_000_000.0,
                                           inp.weeks[0] - dt.timedelta(days=7))
        for t in fres.trades + fterm.liquidation_trades:
            assert t.transaction_cost == pytest.approx(t.gross_traded_value * tc_bps / 10000, rel=1e-12)
            assert t.slippage == pytest.approx(t.gross_traded_value * slip_bps / 10000, rel=1e-12)
            seen.add(t.reason)
        assert fterm.pre_terminal_nav - fterm.nav_after_liquidation == pytest.approx(
            fterm.terminal_trading_costs, rel=1e-9)
    assert {TradeReason.SIGNAL_EXIT, TradeReason.SIGNAL_REENTRY, TradeReason.CALENDAR_REBALANCE,
            TradeReason.BAND_REBALANCE, TradeReason.SELL_TO_PAY, TradeReason.TERMINAL_LIQUIDATION,
            TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION} <= seen
