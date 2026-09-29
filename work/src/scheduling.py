"""Delay scheduling (SIG-016, DELAY-002/004, Q-043): each confirmed transition in week T is
executed at the start of week T + delay (delay=1 => T+1, no hidden extra lag). Executions
are processed FIFO; an execution whose target equals the current effective state is a no-op."""
from __future__ import annotations

import datetime as dt

from .availability import execution_week
from .models import ScheduledExecution, State


class ExecutionQueue:
    def __init__(self, asset: str, effective_state: State = State.RISK_ON, pending=()):
        self.asset = asset
        self.effective_state = effective_state
        self._pending = list(pending)

    @property
    def pending(self) -> tuple:
        return tuple(self._pending)

    def schedule(self, confirm_week: dt.date, delay: int, target: State) -> ScheduledExecution:
        ex = ScheduledExecution(self.asset, confirm_week, execution_week(confirm_week, delay), target)
        self._pending.append(ex)
        return ex

    def due(self, week: dt.date) -> list:
        """Pop executions with execution_week <= week in confirmation order.
        Returns [(execution, is_noop)] and updates the effective state."""
        out, keep = [], []
        for ex in self._pending:
            if ex.execution_week <= week:
                noop = ex.target_state == self.effective_state
                self.effective_state = ex.target_state
                out.append((ex, noop))
            else:
                keep.append(ex)
        self._pending = keep
        return out
