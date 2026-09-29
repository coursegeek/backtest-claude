"""RISK_ON/RISK_OFF state machine with N-week confirmation (SIG-011..017) and gap-aware
counters (Q-012 point 4: observations separated by a missing calendar week are not
consecutive, so every counter restarts)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .calendar import consecutive
from .models import Condition, SignalParams, State


@dataclass(frozen=True)
class MachineState:
    state: State = State.RISK_ON
    basis: str = "fallback"          # fallback until the first confirmed transition (SIG-003)
    exit_counter: int = 0
    entry_counter: int = 0
    prev_key: Optional[dt.date] = None


def step(ms: MachineState, key: dt.date, cond: Condition, params: SignalParams):
    """Advance one observed week. Returns (new_state, confirmed_target_or_None, flags)."""
    flags = []
    exit_c, entry_c = ms.exit_counter, ms.entry_counter
    if ms.prev_key is not None and not consecutive(ms.prev_key, key):
        flags.append("calendar_gap")                      # Q-012/Q-050: always flagged
        if exit_c or entry_c:
            flags.append("counter_reset_gap")
        exit_c = entry_c = 0
    state, basis, confirmed = ms.state, ms.basis, None
    if state == State.RISK_ON:
        entry_c = 0
        exit_c = exit_c + 1 if cond == Condition.BELOW else 0          # SIG-015
        if exit_c >= params.confirm_off:
            state, basis, confirmed, exit_c = State.RISK_OFF, "confirmed", State.RISK_OFF, 0
    else:
        exit_c = 0
        entry_c = entry_c + 1 if cond == Condition.ABOVE else 0
        if entry_c >= params.confirm_on:
            state, basis, confirmed, entry_c = State.RISK_ON, "confirmed", State.RISK_ON, 0
    return MachineState(state, basis, exit_c, entry_c, key), confirmed, tuple(flags)
