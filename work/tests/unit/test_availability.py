import datetime as dt

import pytest

from fixtures.builders import price_series
from src.availability import DataView, decision_time, evaluation_time, execution_week
from src.errors import ConfigError, LookAheadError

D = dt.date.fromisoformat


def test_time_model():
    k = D("2024-01-05")
    assert evaluation_time(k) == D("2024-01-07")          # Sunday end of week
    assert decision_time(k) == D("2023-12-31")            # end of previous week
    assert execution_week(k, 1) == D("2024-01-12")
    for bad in (0, -1, 1.5):
        with pytest.raises(ConfigError):
            execution_week(k, bad)


def test_dataview_blocks_future():
    s = price_series([1, 2, 3], avail_days=2)             # BTC-like availability
    v = DataView(s.points)
    assert v.visible(s.points[1].week_key) == s.points[:1]
    assert v.get(s.points[1].week_key, s.points[1].available_at).price == 2
    with pytest.raises(LookAheadError):
        v.get(s.points[2].week_key, s.points[1].available_at)


def test_btc_return_week_assignment():
    """NORM-017: the Sunday-to-Sunday return ending in close_date belongs to that week key."""
    s = price_series([100, 110], avail_days=2)
    r = s.returns()[0]
    assert r.week_key == s.points[1].week_key and r.available_at == s.points[1].available_at
    assert abs(r.value - 0.1) < 1e-15
