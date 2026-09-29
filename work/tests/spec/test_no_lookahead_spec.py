"""TEST-018, TEST-051 (TEST-002 needs the portfolio engine and is not implemented yet)."""
import datetime as dt
import shutil

import pytest

from fixtures.builders import STAGED, price_series, write_csv
from src.app import run_signals
from src.availability import DataView, evaluation_time
from src.config import ResolvedConfig
from src.data_loader import load_btc
from src.errors import LookAheadError
from src.models import PricePoint, SignalParams, State
from src.signal_analysis import evaluate

WEEK = dt.timedelta(days=7)


def btc_raw_rows(prices, first_monday="2020-01-06"):
    m0 = dt.date.fromisoformat(first_monday)
    rows = []
    for i, p in enumerate(prices):
        m = m0 + i * WEEK
        wr = "" if i == 0 else repr(p / prices[i - 1] - 1)
        rows.append([m.isoformat(), repr(p), wr, (m + dt.timedelta(days=6)).isoformat(),
                     m.isoformat(), "fixture"])
    return rows


def test_btc_sunday_close_availability(tmp_path):
    """TEST-018 / NORM-016, SEM-009: the Sunday close is never known on the Friday key; a
    signal confirmed on record K executes at the earliest in week K+7, never on Friday K."""
    prices = [100.0] * 5 + [80.0, 80.0]
    path = write_csv(tmp_path / "btc.csv", ["date", "price", "weekly_return", "close_date",
                                            "source_week_start", "source"], btc_raw_rows(prices))
    s = load_btc(path)
    k = s.points[5].week_key
    assert k.weekday() == 4 and s.points[5].available_at == k + dt.timedelta(days=2)
    view = DataView(s.points)
    assert s.points[5] not in view.visible(k)                   # not visible on Friday K
    assert s.points[5] in view.visible(evaluation_time(k))      # visible at end of week K
    with pytest.raises(LookAheadError):
        view.get(k, k)
    p = SignalParams("btc", 5, 0.03, 0.03, 1, 1, 1, 0.5, "sell_fraction_current")
    recs, _, _ = evaluate(s, p)
    conf = [r for r in recs if r.confirmation]
    assert conf[0].week_key == k and conf[0].scheduled_execution_week == k + WEEK
    assert recs[5].effective_state == State.RISK_ON and recs[6].effective_state == State.RISK_OFF
    # an observation that would only be available after the evaluation time is rejected
    bad = s.replace_points([*s.points[:-1], PricePoint(s.points[-1].week_key, 80.0,
                                                       s.points[-1].week_key + dt.timedelta(days=3),
                                                       s.points[-1].source_date)])
    with pytest.raises(LookAheadError):
        evaluate(bad, p)


def _stocks_with_partial_week(tmp_path):
    src = STAGED / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"
    path = tmp_path / "stocks.csv"
    shutil.copy(src, path)
    with path.open("a", encoding="utf-8") as f:
        f.write("2026-09-21,178000.0000000000\n")
    return path


@pytest.mark.parametrize("as_of,kept", [("2026-09-22", False), ("2026-09-25", True)])
def test_incomplete_current_week_drop(tmp_path, as_of, kept):
    """TEST-051 / NORM-019, NORM-020, SEM-011: with as_of 2026-09-22 the week ending
    2026-09-25 is dropped and never reaches SMA/confirmation; from 2026-09-25 it is kept."""
    path = _stocks_with_partial_week(tmp_path)
    cfg = ResolvedConfig("signals", cli_layer={
        "data": {"stocks_price_file": str(path)}, "run": {"asset": "stocks", "as_of_date": as_of,
                                                          "start": "2026-01-02"}})
    res = run_signals(cfg, write=False)
    weeks = [r.week_key for r in res.records]
    assert (dt.date(2026, 9, 25) in weeks) is kept
    assert (res.dropped_incomplete_weeks == 1) is (not kept)
    if not kept:
        assert any(i.code == "incomplete_week_dropped" and i.week_key == dt.date(2026, 9, 25)
                   for i in res.report.issues)
        # the SMA of the last kept week is computed without the partial week
        assert weeks[-1] == dt.date(2026, 9, 18)


def test_signal_T_not_affect_return_T():
    """TEST-002 / META-003, PORT-012: an exit confirmed at the end of week T leaves the
    exposure of week T unchanged; with delay=1 it acts only before the return of T+1."""
    from fixtures.builders import engine_inputs, params_for
    from src.engine import run_engine
    hist = [100.0] * 6 + [90.0, 99.0, 98.0, 97.0]         # below SMA from index 6
    rets = [0.0, -0.10, 0.05, -0.02, 0.03]                 # run weeks = indexes 5..9
    base = dict(histories={"stocks": hist}, first=5, targets={"stocks": 0.8, "rf": 0.2},
                returns={"stocks": rets}, rf=0.001)
    sig = run_engine(engine_inputs(**base))
    quiet = run_engine(engine_inputs(**base, params={"stocks": params_for("stocks", confirm_off=99)}))
    conf = [r for r in sig.signal_records if r.confirmation]
    T = conf[0].week_key
    assert T == sig.weeks[1].week_key                     # confirmed at the end of week index 6
    for a, b in zip(sig.weeks[:2], quiet.weeks[:2]):      # up to and including week T
        assert a.nav_end == b.nav_end and a.trades == ()
    w1 = sig.weeks[1]
    assert w1.ledger_after_trades.stocks == w1.ledger_start.stocks      # return T on pre-signal exposure
    w2 = sig.weeks[2]
    assert [t.reason.value for t in w2.trades] == ["signal_exit"] and w2.week_key == T + WEEK
    assert w2.ledger_after_trades.stocks == w2.ledger_start.stocks * 0.5
    assert sig.weeks[2].nav_end != quiet.weeks[2].nav_end
