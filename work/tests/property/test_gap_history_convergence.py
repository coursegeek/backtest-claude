"""Q-012: whole history is used; for starts well after the 1933 closure the reconstructed
stock state equals the state reconstructed from post-gap history only (evidence that the
gap does not distort later signals)."""
import datetime as dt

import pytest

from src.config import ResolvedConfig
from src.data_loader import load_role
from src.signal_analysis import reconstruct


@pytest.mark.parametrize("start", ["1936-01-03", "1950-01-06", "1971-01-01"])
def test_state_independent_of_pre_gap_history(start):
    cfg = ResolvedConfig()
    s = load_role(cfg, "stocks_price")
    p = cfg.signal_params("stocks")
    first = dt.date.fromisoformat(start)
    full = reconstruct(s, p, first)
    post = s.replace_points([x for x in s.points if x.week_key > dt.date(1933, 3, 10)])
    tail = reconstruct(post, p, first)
    assert (full.confirmed_state, full.effective_state, full.exit_counter, full.entry_counter) == \
        (tail.confirmed_state, tail.effective_state, tail.exit_counter, tail.entry_counter)
    assert full.history_weeks > tail.history_weeks
