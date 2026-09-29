"""Signal indicators: SMA, per-asset threshold bands and raw conditions
(SIG-001, SIG-004..007, THR-004)."""
from __future__ import annotations

import math
from typing import Optional, Sequence

from .models import Condition


def sma(prices: Sequence[float], ma: int) -> list:
    """SMA_t = mean(price[t-ma+1 .. t]) over the last ``ma`` observations including t
    (inclusive window, Q-028); None until ``ma`` observations exist. math.fsum keeps every
    window exact and independent of summation history."""
    if ma < 2:
        raise ValueError("ma must be >= 2 (SIG-001)")
    out = [None] * len(prices)
    for t in range(ma - 1, len(prices)):
        out[t] = math.fsum(prices[t - ma + 1:t + 1]) / ma
    return out


def bands(sma_value: Optional[float], threshold_off: float, threshold_on: float):
    """Lower band SMA*(1-p_off) (SIG-004) and upper band SMA*(1+p_on) (SIG-005)."""
    if sma_value is None:
        return None, None
    return sma_value * (1.0 - threshold_off), sma_value * (1.0 + threshold_on)


def classify(price: float, lower: Optional[float], upper: Optional[float]) -> Condition:
    """Raw weekly condition. Inside the closed band [lower, upper] the state is kept
    (hysteresis, SIG-006); threshold 0 gives strict price<SMA / price>SMA (THR-004)."""
    if lower is None:
        return Condition.NO_SMA
    if price < lower:
        return Condition.BELOW
    if price > upper:
        return Condition.ABOVE
    return Condition.INSIDE
