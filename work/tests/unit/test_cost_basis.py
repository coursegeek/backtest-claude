import datetime as dt

import pytest

from src.cost_basis import CostBasisBook
from src.errors import ConfigError

W = [dt.date(2020, 1, 3) + dt.timedelta(days=7 * i) for i in range(5)]


def book(method="FIFO"):
    b = CostBasisBook(method)
    b.open_lot("stocks", W[0], 100.0, 100.0, "initial")      # 100 units @ 1.00
    b.open_lot("stocks", W[1], 50.0, 75.0, "buy")            # 50 units @ 1.50
    return b


def test_fifo_and_average():
    """PORT-008, IND-008, IND-011: FIFO consumes the oldest lot first; average cost reduces
    every lot proportionally; realised gain = net proceeds - basis."""
    b = book()
    r = b.sell_fraction("stocks", 120.0 / 150.0, W[2], proceeds_net=240.0)   # 120 units
    assert r.units_sold == 120.0 and r.cost_basis == 100.0 + 20 * 1.5
    assert r.realized_gain == 240.0 - 130.0
    assert [(l.lot_id, l.units, l.cost) for l in b.lots("stocks")] == [(2, 30.0, 45.0)]
    a = book("average_cost")
    r = a.sell_fraction("stocks", 0.2, W[2], proceeds_net=60.0)
    assert abs(r.cost_basis - 0.2 * 175.0) < 1e-12 and abs(a.cost("stocks") - 0.8 * 175.0) < 1e-12


def test_sell_all_and_copy():
    b = book()
    snapshot = b.copy()
    r = b.sell_fraction("stocks", 1.0, W[3], proceeds_net=300.0)
    assert r.cost_basis == 175.0 and b.lots("stocks") == () and b.units("stocks") == 0
    assert snapshot.units("stocks") == 150.0                 # copies are independent


def test_invalid_inputs():
    with pytest.raises(ConfigError):
        CostBasisBook("LIFO")
    with pytest.raises(ValueError):
        book().sell_fraction("stocks", 0.0, W[2], 0.0)
