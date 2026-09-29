"""TEST-005, TEST-006."""
import pytest

from fixtures.builders import engine_inputs, params_for
from src.engine import run_engine
from src.models import State

HIST = [100.0] * 4 + [110.0, 80.0, 70.0, 60.0, 65.0, 62.0]    # exit confirmed at index 5


def test_sell_fraction_half_only_on_transition():
    """TEST-005 / RISK-001, RISK-007: exactly 50% of the CURRENT position is sold, only on the
    RISK_ON -> RISK_OFF execution; staying in RISK_OFF triggers no further sale."""
    res = run_engine(engine_inputs({"stocks": HIST}, first=4, targets={"stocks": 1.0},
                                   returns={"stocks": [0.10, -0.2, 0.05, -0.1, 0.02, 0.01]}))
    sells = [t for t in res.trades if t.side == "sell"]
    assert len(sells) == 1 and len(res.trades) == 1
    t = sells[0]
    week = next(w for w in res.weeks if w.week_key == t.week_key)
    assert t.asset_value_before == week.ledger_start.stocks                 # current value, drifted
    assert t.asset_value_before != res.initial_ledger.stocks
    assert t.gross_traded_value == t.asset_value_before * 0.5
    assert t.asset_value_after == t.asset_value_before - t.gross_traded_value
    later = [w for w in res.weeks if w.week_key > t.week_key]
    assert later and all(w.trades == () for w in later)
    assert all(w.effective_states["stocks"] == State.RISK_OFF for w in later)


def test_proceeds_to_own_reserve():
    """TEST-006 / RISK-002, RISK-003: proceeds go only to rf_reserve_<asset>; other assets,
    their reserves and rf_base are untouched by the signal trade."""
    flat = [100.0] * 10
    res = run_engine(engine_inputs({"stocks": HIST, "gold": flat, "btc": flat}, first=4,
                                   targets={"stocks": 0.4, "gold": 0.3, "btc": 0.2, "rf": 0.1},
                                   returns={"stocks": [0.1, -0.2, 0.05, -0.1, 0.02, 0.01],
                                            "gold": [0.01] * 6, "btc": [0.03] * 6}, rf=0.002))
    t = res.trades[0]
    assert (t.asset, t.cash_component, len(res.trades)) == ("stocks", "rf_reserve_stocks", 1)
    week = next(w for w in res.weeks if w.week_key == t.week_key)
    before, after = week.ledger_start, week.ledger_after_signal
    assert after.rf_reserve_stocks == before.rf_reserve_stocks + t.net_cash_flow
    for c in ("gold", "btc", "rf_base", "rf_reserve_gold", "rf_reserve_btc"):
        assert getattr(after, c) == getattr(before, c)
