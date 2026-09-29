import datetime as dt

from fixtures.builders import price_series
from src.confirmation import MachineState, step
from src.models import Condition, SignalParams, State
from src.signal_analysis import evaluate

D = dt.date.fromisoformat
P = SignalParams("x", 2, 0.0, 0.0, 3, 2, 1, 0.5, "sell_fraction_current")


def run(conds, params=P):
    ms, out = MachineState(), []
    for i, c in enumerate(conds):
        ms, conf, _ = step(ms, D("2020-01-03") + dt.timedelta(days=7 * i), c, params)
        out.append((ms.state, ms.exit_counter, ms.entry_counter, conf))
    return out


def test_counter_resets_on_break():
    """SIG-015."""
    B, I, A = Condition.BELOW, Condition.INSIDE, Condition.ABOVE
    out = run([B, B, I, B, B, B])
    assert [o[1] for o in out] == [1, 2, 0, 1, 2, 0]
    assert out[-1][0] == State.RISK_OFF and out[-1][3] == State.RISK_OFF
    out = run([B, B, B, A, B, A, A])
    assert [o[2] for o in out[3:]] == [1, 0, 1, 0]
    assert out[-1][0] == State.RISK_ON


def test_separate_off_on_confirmation():
    """SIG-017: exit needs confirm_off (3), re-entry confirm_on (2)."""
    B, A = Condition.BELOW, Condition.ABOVE
    out = run([B, B, B, A, A])
    assert [o[3] for o in out] == [None, None, State.RISK_OFF, None, State.RISK_ON]


def test_no_sma_resets_counters():
    out = run([Condition.BELOW, Condition.NO_SMA, Condition.BELOW])
    assert [o[1] for o in out] == [1, 0, 1]


def test_assets_independent():
    """SIG-008: each asset has its own state machine and parameters."""
    s = price_series([100, 100, 100, 90, 80, 70])        # 3 weeks below a 2-week SMA
    a, _, _ = evaluate(s, SignalParams("stocks", 2, 0.0, 0.0, 2, 2, 1, 0.5, "sell_fraction_current"))
    b, _, _ = evaluate(s, SignalParams("gold", 2, 0.0, 0.0, 4, 4, 1, 0.5, "sell_fraction_current"))
    assert any(r.confirmation for r in a) and not any(r.confirmation for r in b)
