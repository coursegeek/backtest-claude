import pytest

from src.costs import CostModel
from src.errors import ConfigError


def test_sale_and_purchase_formulas():
    """Q-017: net = traded*(1-tc-slip); purchase traded = C/(1+tc+slip)."""
    m = CostModel(10.0, 5.0)
    cost, slip, net = m.sale(100_000.0)
    assert (cost, slip) == (100.0, 50.0) and net == 100_000.0 * (1 - 0.0015)
    traded, cost, slip = m.purchase_from_cash(100_150.0)
    assert abs(traded - 100_000.0) < 1e-9 and abs(traded + cost + slip - 100_150.0) < 1e-9


def test_zero_bps_is_exact():
    m = CostModel()
    assert m.sale(123.456) == (0.0, 0.0, 123.456)
    assert m.purchase_from_cash(123.456) == (123.456, 0.0, 0.0)


def test_negative_bps_rejected():
    with pytest.raises(ConfigError):
        CostModel(-1.0, 0.0)
