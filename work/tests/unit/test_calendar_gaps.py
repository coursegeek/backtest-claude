"""Q-012 adjudicated calendar-gap semantics."""
import datetime as dt

import pytest

from fixtures.builders import price_series
from src.errors import DataValidationError
from src.models import Condition, SignalParams, State
from src.signal_analysis import evaluate, reconstruct
from src.validation import build_run_calendar

D = dt.date.fromisoformat
P = SignalParams("stocks", 3, 0.0, 0.0, 2, 2, 1, 0.5, "sell_fraction_current")


def k(i, first="1933-01-06"):
    return D(first) + dt.timedelta(days=7 * i)


def test_common_calendar_gap_reported_not_filled():
    """Q-012 (1,2,6): a week absent from every source is reported and skipped, never filled,
    and the policy is not triggered even with error defaults."""
    src = {"stocks_return": [k(i) for i in range(6) if i != 3],
           "stocks_price": [k(i) for i in range(6) if i != 3]}
    cal = build_run_calendar(k(0), k(5), src, {"stocks_return"}, {"stocks_price"})
    assert cal.common_gaps == (k(3),) and k(3) not in cal.weeks
    assert cal.dropped == () and cal.carried == ()
    assert [i.code for i in cal.issues] == ["common_calendar_gap"]


def test_single_source_missing_applies_policy():
    """Q-012 (5,7): one required source missing while others have the week -> policy."""
    src = {"stocks_return": [k(i) for i in range(6)], "stocks_price": [k(i) for i in range(6) if i != 3]}
    with pytest.raises(DataValidationError):
        build_run_calendar(k(0), k(5), src, {"stocks_return"}, {"stocks_price"})
    dropped = build_run_calendar(k(0), k(5), src, {"stocks_return"}, {"stocks_price"}, return_policy="drop")
    assert dropped.dropped == (k(3),) and dropped.common_gaps == ()


def test_confirmation_interrupted_by_calendar_gap():
    """Q-012 (4): observations separated by a missing week are not consecutive; counters
    restart, so two below-SMA weeks around a gap do not confirm (confirm_off=2)."""
    prices = [100, 100, 100, 90, 0, 90, 100, 100]
    s = price_series(prices, first_key="1933-01-06", skip=(4,))
    recs, _, _ = evaluate(s, P)
    by = {r.week_key: r for r in recs}
    assert by[k(3)].condition == Condition.BELOW and by[k(3)].exit_counter == 1
    assert by[k(5)].exit_counter == 1 and "counter_reset_gap" in by[k(5)].flags
    assert not any(r.confirmation for r in recs)
    contiguous = price_series([100, 100, 100, 90, 90, 90], first_key="1933-01-06")
    recs, _, _ = evaluate(contiguous, P)
    assert [r.week_key for r in recs if r.confirmation] == [k(4)]


def test_reconstruction_uses_history_before_gap():
    """Q-012 (3): state reconstruction uses all history, including observations before a gap
    (no longest-contiguous-segment cut)."""
    prices = [100] * 3 + [80, 80] + [0] + [80] * 2
    s = price_series(prices, first_key="1933-01-06", skip=(5,))
    st = reconstruct(s, P, k(8))
    assert st.history_weeks == 7 and st.confirmed_state == State.RISK_OFF
    assert st.state_basis == "confirmed"


def test_price_carry_only_when_configured():
    """Q-012 (7): carry happens only with missing.price_policy=carry."""
    src = {"stocks_return": [k(i) for i in range(4)], "gold": [k(i) for i in range(4) if i != 2]}
    with pytest.raises(DataValidationError):
        build_run_calendar(k(0), k(3), src, {"stocks_return"}, {"gold"}, price_policy="error")
    cal = build_run_calendar(k(0), k(3), src, {"stocks_return"}, {"gold"}, price_policy="carry")
    assert cal.carried == (("gold", k(2)),)


def test_q050_sma_over_available_observations_across_gap():
    """Q-050: SMA = mean of the last ma available observations (the window spans the gap, no
    fill, no reset of SMA history); the gap is flagged; counters restart; warm-up counts
    available observations."""
    from src.validation import check_warmup
    prices = [10.0, 20.0, 30.0, 0.0, 40.0, 50.0]
    s = price_series(prices, first_key="1933-01-06", skip=(3,))      # week 3 missing
    p = SignalParams("stocks", 3, 0.0, 0.0, 2, 2, 1, 0.5, "sell_fraction_current")
    recs, _, _ = evaluate(s, p)
    by = {r.week_key: r for r in recs}
    after = by[k(4)]
    assert after.sma == (20.0 + 30.0 + 40.0) / 3                       # spans the gap
    assert "calendar_gap" in after.flags and "sma_spans_gap" in after.flags
    assert by[k(5)].sma == (30.0 + 40.0 + 50.0) / 3 and "calendar_gap" not in by[k(5)].flags
    assert by[k(2)].sma == 20.0 and "sma_spans_gap" not in by[k(2)].flags
    # warm-up need = ma + confirm + delay = 6; six calendar weeks elapsed but only five
    # observations exist before k(6) -> insufficient; one more observation satisfies it.
    from src.errors import WarmupError
    with pytest.raises(WarmupError) as exc:
        check_warmup("stocks", s.keys(), k(6), p, "reconstruct_history")
    assert (exc.value.available, exc.value.required) == (5, 6)
    assert check_warmup("stocks", s.keys() + (k(6),), k(7), p, "reconstruct_history") is None
