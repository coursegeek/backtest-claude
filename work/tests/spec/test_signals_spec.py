"""TEST-001, TEST-003, TEST-004, TEST-031, TEST-040, TEST-041, TEST-042."""
import datetime as dt
import math

import pytest

from fixtures.builders import price_series
from src.allocation import initial_sleeves
from src.config import ResolvedConfig
from src.errors import WarmupError
from src.models import Condition, SignalParams, State
from src.signal_analysis import evaluate, reconstruct
from src.signals import sma
from src.validation import check_warmup

WEEK = dt.timedelta(days=7)


def params(**kw):
    base = dict(asset="stocks", ma=5, threshold_off=0.0, threshold_on=0.0, confirm_off=1,
                confirm_on=1, delay=1, sell_fraction=0.5, risk_off_action="sell_fraction_current")
    base.update(kw)
    return SignalParams(**base)


def test_sma50_manual():
    """TEST-001 / SIG-001: SMA50 equals the hand-computed mean of the last 50 weeks."""
    prices = [100.0 + t for t in range(60)]
    s = sma(prices, 50)
    assert all(v is None for v in s[:49])
    assert s[49] == 124.5 and s[59] == 134.5
    for t in range(49, 60):
        manual = 0.0
        for x in prices[t - 49:t + 1]:
            manual += x
        assert math.isclose(s[t], manual / 50, rel_tol=0, abs_tol=1e-12)


def test_confirmation_delay_pipeline():
    """TEST-003 / SIG-016 / DELAY-002/004: the N-th qualifying week confirms; delay=1 executes
    at the start of the next week, delay=2 one week later; a broken run does not confirm."""
    base = [100.0] * 10
    prices = base + [90.0, 90.0, 90.0, 90.0]     # weeks 10..12 below the flat SMA
    series = price_series(prices)
    keys = series.keys()
    for delay, expected_exec in ((1, keys[13]), (2, keys[12] + 2 * WEEK)):
        recs, _, _ = evaluate(series, params(ma=5, confirm_off=3, delay=delay))
        conf = [r for r in recs if r.confirmation]
        assert [r.week_key for r in conf] == [keys[12]]
        assert conf[0].scheduled_execution_week == expected_exec
        assert recs[12].effective_state == State.RISK_ON      # not executed in week T
        if delay == 1:
            assert recs[13].effective_state == State.RISK_OFF
            assert recs[13].executed_target == State.RISK_OFF
    broken = base + [90.0, 101.0, 90.0, 90.0]
    recs, _, _ = evaluate(price_series(broken), params(ma=5, confirm_off=3))
    assert not any(r.confirmation for r in recs)


def test_hysteresis_3pct():
    """TEST-004 / SIG-006: inside the +-3% band the state is kept; the band edges are inside."""
    p = params(ma=5, threshold_off=0.03, threshold_on=0.03)
    prices = [100.0] * 5 + [97.0, 103.0, 97.0, 103.0]
    recs, _, _ = evaluate(price_series(prices), p)
    assert all(r.condition in (Condition.INSIDE, Condition.NO_SMA) for r in recs)
    assert all(r.confirmed_state == State.RISK_ON for r in recs)
    # flat SMA of 100, then 96 < lower band 97 -> exit condition (confirm_off=1)
    recs, _, _ = evaluate(price_series([100.0] * 5 + [96.0]), p)
    assert recs[-1].condition == Condition.BELOW and recs[-1].confirmed_state == State.RISK_OFF


def _first_confirmation(series, p):
    recs, _, _ = evaluate(series, p)
    return next(r for r in recs if r.confirmation)


def test_confirmation_defaults():
    """TEST-031 / SIG-012/013: stocks confirm after 2 weeks, gold after 4, whatever the delay;
    delay only shifts the execution week."""
    cfg = ResolvedConfig()
    stocks, gold = cfg.signal_params("stocks"), cfg.signal_params("gold")
    assert (stocks.confirm_off, stocks.confirm_on, gold.confirm_off, gold.confirm_on) == (2, 2, 4, 4)
    prices = [100.0] * 50 + [80.0] * 8
    series = price_series(prices)
    keys = series.keys()
    for delay in (1, 3):
        s = _first_confirmation(series, SignalParams(**{**stocks.__dict__, "delay": delay}))
        g = _first_confirmation(series, SignalParams(**{**gold.__dict__, "delay": delay}))
        assert s.week_key == keys[51] and g.week_key == keys[53]
        assert s.scheduled_execution_week == keys[51] + delay * WEEK
        assert g.scheduled_execution_week == keys[53] + delay * WEEK


def test_default_thresholds():
    """TEST-040 / SIG-004, DEF-003, DEF-030..032."""
    cfg = ResolvedConfig()
    assert cfg.signal_params("stocks").threshold_off == 0.0
    assert cfg.signal_params("gold").threshold_off == 0.0
    assert cfg.signal_params("btc").threshold_off == 0.03
    new = cfg.signal_params("new_asset")
    assert (new.threshold_off, new.threshold_on) == (0.03, 0.03)


def test_initial_state_reconstruction():
    """TEST-041 / SIG-003, SIG-018: history ending in RISK_OFF initialises the sleeve split
    (asset 50%, reserve 50% with sell_fraction 0.5); no reset to RISK_ON."""
    prices = [100.0] * 50 + [70.0] * 10
    series = price_series(prices)
    p = params(ma=50, confirm_off=2, confirm_on=2, delay=1)
    first = series.keys()[-1] + WEEK
    st = reconstruct(series, p, first)
    assert st.confirmed_state == State.RISK_OFF and st.effective_state == State.RISK_OFF
    assert st.state_basis == "confirmed" and st.pending == ()
    sleeves = initial_sleeves(1_000_000.0, {"stocks": 1.0, "gold": 0.0, "btc": 0.0, "rf": 0.0},
                              {"stocks": st.effective_state}, {"stocks": p})
    s = sleeves.sleeve("stocks")
    assert (s.asset_value, s.reserve_value, sleeves.rf_base) == (500_000.0, 500_000.0, 0.0)


def test_warmup_confirmation_delay():
    """TEST-042 / NORM-010, ERR-003: minimum warm-up = ma + max(confirm) + max(delay); one
    week less fails; the earliest confirmation is exactly at 0-based index ma + N - 2."""
    p = params(ma=10, confirm_off=3, confirm_on=2, delay=2)
    assert p.minimum_warmup_weeks == 10 + 3 + 2
    series = price_series([200.0 - t for t in range(40)])
    keys = series.keys()
    assert check_warmup("stocks", keys, keys[15], p, "reconstruct_history") is None
    with pytest.raises(WarmupError) as exc:
        check_warmup("stocks", keys, keys[14], p, "reconstruct_history")
    assert exc.value.available == 14 and exc.value.required == 15
    warn = check_warmup("stocks", keys, keys[14], p, "RISK_ON")
    assert warn is not None and warn.code == "warmup_short"
    recs, _, _ = evaluate(series, p)
    first = next(i for i, r in enumerate(recs) if r.confirmation)
    assert first == p.ma + p.confirm_off - 2
    assert recs[first].scheduled_execution_week == keys[first] + 2 * WEEK
