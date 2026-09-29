import datetime as dt

from fixtures.builders import price_series
from src.models import SignalParams, State
from src.scheduling import ExecutionQueue
from src.signal_analysis import evaluate, reconstruct

D = dt.date.fromisoformat
W = dt.timedelta(days=7)


def test_delay_one_exact_shift():
    """DELAY-004: delay=1 executes exactly one week after confirmation, no hidden lag."""
    q = ExecutionQueue("stocks")
    ex = q.schedule(D("2020-01-03"), 1, State.RISK_OFF)
    assert ex.execution_week == D("2020-01-10")
    assert q.due(D("2020-01-03")) == [] and q.effective_state == State.RISK_ON
    assert q.due(D("2020-01-10")) == [(ex, False)] and q.effective_state == State.RISK_OFF


def test_fifo_and_noop():
    """Q-043: opposite confirmations pending together execute FIFO; a redundant one is a no-op."""
    q = ExecutionQueue("btc")
    a = q.schedule(D("2020-01-03"), 4, State.RISK_OFF)
    b = q.schedule(D("2020-01-10"), 4, State.RISK_ON)
    c = q.schedule(D("2020-01-17"), 4, State.RISK_ON)
    assert q.due(D("2020-02-14")) == [(a, False), (b, False), (c, True)]
    assert q.effective_state == State.RISK_ON and q.pending == ()


def test_execution_week_in_gap_runs_next_observed_week():
    q = ExecutionQueue("stocks")
    ex = q.schedule(D("1933-03-03"), 1, State.RISK_OFF)      # 1933-03-10 is a closed week
    assert q.due(D("1933-03-17")) == [(ex, False)]


def test_reconstruction_keeps_pending_executions():
    """Q-019: an execution scheduled before the start but due at/after it stays pending; the
    initial split follows the effective (executed) state."""
    s = price_series([100.0] * 5 + [80.0, 80.0])
    p = SignalParams("stocks", 5, 0.0, 0.0, 2, 2, 3, 0.5, "sell_fraction_current")
    last = s.keys()[-1]
    st = reconstruct(s, p, last + W)
    assert st.confirmed_state == State.RISK_OFF and st.effective_state == State.RISK_ON
    assert [x.execution_week for x in st.pending] == [last + 3 * W]
    st2 = reconstruct(s, p, last + 4 * W)
    assert st2.effective_state == State.RISK_OFF and st2.pending == ()


def test_evaluate_until_is_prefix_of_full_run():
    s = price_series([100.0 + (i % 7) * 3 - i * 0.5 for i in range(60)])
    p = SignalParams("stocks", 10, 0.01, 0.01, 2, 2, 2, 0.5, "sell_fraction_current")
    full, _, _ = evaluate(s, p)
    part, _, _ = evaluate(s, p, until=s.keys()[40])
    assert full[:40] == part
