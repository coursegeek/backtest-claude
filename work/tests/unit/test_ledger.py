import math

import pytest

from src.ledger import COMPONENTS, Ledger, LedgerInvariantError
from src.models import PortfolioSleeves, Sleeve


def test_components_nav_and_sleeves():
    """PORT-007, PORT-009, Q-049: explicit components; NAV built with fsum; sleeve = asset +
    its reserve."""
    assert COMPONENTS == ("stocks", "gold", "btc", "rf_base", "rf_reserve_stocks",
                          "rf_reserve_gold", "rf_reserve_btc")
    l = Ledger(stocks=0.1, gold=0.2, btc=0.3, rf_base=1e16, rf_reserve_stocks=0.4)
    assert l.nav == math.fsum([0.1, 0.2, 0.3, 1e16, 0.4, 0.0, 0.0])
    assert l.sleeve("stocks") == 0.1 + 0.4 and l.sleeve("btc") == 0.3
    l.check()


def test_sleeve_totals():
    """PORT-009 via construction from allocation sleeves."""
    l = Ledger.from_sleeves(PortfolioSleeves((Sleeve("stocks", 60.0, 40.0), Sleeve("gold", 30.0, 0.0)), 10.0))
    assert (l.sleeve("stocks"), l.sleeve("gold"), l.rf_base, l.nav) == (100.0, 30.0, 10.0, 140.0)
    assert l.sleeve_weights() == {"stocks": 100 / 140, "gold": 30 / 140, "btc": 0.0, "rf": 10 / 140}


@pytest.mark.parametrize("bad", [{"gold": -1e-9}, {"rf_reserve_btc": float("nan")},
                                 {"btc": float("inf")}])
def test_invariants_reject_negative_and_non_finite(bad):
    with pytest.raises(LedgerInvariantError):
        Ledger(stocks=1.0, **bad).check()
