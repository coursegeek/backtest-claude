import pytest

from src.models import Condition
from src.signals import bands, classify, sma


def test_sma_requires_ma_ge_2():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 1)


def test_upper_band_reentry_condition():
    """SIG-005."""
    lo, hi = bands(100.0, 0.03, 0.03)
    assert classify(103.01, lo, hi) == Condition.ABOVE and classify(103.0, lo, hi) == Condition.INSIDE


def test_asymmetric_thresholds():
    """SIG-007."""
    lo, hi = bands(100.0, 0.02, 0.05)
    assert (lo, hi) == (98.0, 105.0)
    assert classify(97.9, lo, hi) == Condition.BELOW and classify(104.9, lo, hi) == Condition.INSIDE


def test_zero_threshold_strict():
    """THR-004: price<SMA -> below, price>SMA -> above, price==SMA keeps state."""
    lo, hi = bands(100.0, 0.0, 0.0)
    assert [classify(p, lo, hi) for p in (99.99, 100.0, 100.01)] == [
        Condition.BELOW, Condition.INSIDE, Condition.ABOVE]
    assert classify(1.0, *bands(None, 0.0, 0.0)) == Condition.NO_SMA
