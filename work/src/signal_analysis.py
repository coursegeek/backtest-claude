"""Signal pipeline: indicators -> confirmation -> scheduling -> effective state, historical
reconstruction before a start week (SIG-003, SIG-016, SIG-018, SIG-019, NORM-010/012/016).

``SignalTracker`` is the single incremental implementation used by signal-only analysis,
pre-start reconstruction and the portfolio engine. Per observed week K of one asset:
  1. start of week K: ``due(K)`` applies executions with execution_week <= K (in the
     portfolio engine these become trades in pipeline step 1);
  2. end of week K (evaluation time, Sunday): ``observe(point)`` requires the observation to
     be available, updates SMA (last ``ma`` available observations, Q-050), bands, condition
     and counters (reset across a calendar gap, Q-012) and schedules a confirmed transition
     at K + delay.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
from collections import OrderedDict, deque
from dataclasses import dataclass

from .availability import assert_available, evaluation_time
from .calendar import WEEK
from .confirmation import MachineState, step
from .models import Severity, SignalParams, SignalRecord, State, ValidationIssue
from .scheduling import ExecutionQueue
from .signals import bands, classify


class SignalTracker:
    """Incremental, deterministic signal state of one asset."""

    def __init__(self, params: SignalParams):
        self.params = params
        self.keys = deque(maxlen=params.ma)         # week keys of the last ma observations
        self.prices = deque(maxlen=params.ma)       # their prices (SMA window)
        self.gaps = 0                               # non-weekly steps inside the window
        self.machine = MachineState()
        self.queue = ExecutionQueue(params.asset, State.RISK_ON)
        self.observed = 0
        self.last_key = None

    def _push(self, key: dt.date, price: float) -> None:
        """Append one observation to the SMA window, keeping the count of calendar gaps
        between consecutive window keys (integer bookkeeping only)."""
        keys = self.keys
        if len(keys) == keys.maxlen and keys[1] - keys[0] != WEEK:
            self.gaps -= 1                          # the leaving pair
        if keys and key - keys[-1] != WEEK:
            self.gaps += 1                          # the entering pair
        keys.append(key)
        self.prices.append(price)

    @property
    def effective_state(self) -> State:
        return self.queue.effective_state

    def due(self, week: dt.date) -> list:
        """Executions whose execution_week <= week, FIFO: [(ScheduledExecution, is_noop)]."""
        return self.queue.due(week)

    def observe(self, point, executed=(), record: bool = True):
        """End-of-week update; returns the SignalRecord (None when ``record`` is False - the
        pre-start reconstruction discards its records, the state update is identical)."""
        p = self.params
        if self.last_key is not None and point.week_key <= self.last_key:
            raise ValueError(f"{p.asset}: observations must be strictly increasing")
        assert_available(point, evaluation_time(point.week_key), f"{p.asset}: ")
        self._push(point.week_key, point.price)
        flags = []
        s = None
        if len(self.prices) == p.ma:
            s = math.fsum(self.prices) / p.ma                    # SIG-001, Q-050
            if self.gaps:
                flags.append("sma_spans_gap")
        lower, upper = bands(s, p.threshold_off, p.threshold_on)
        cond = classify(point.price, lower, upper)
        self.machine, confirmed, step_flags = step(self.machine, point.week_key, cond, p)
        scheduled = None
        if confirmed is not None:
            scheduled = self.queue.schedule(point.week_key, p.delay, confirmed).execution_week
        if not record:
            self.observed += 1
            self.last_key = point.week_key
            return None
        exec_target = next((ex.target_state for ex, noop in reversed(executed) if not noop), None)
        all_flags = (tuple(step_flags) + tuple(flags)
                     + tuple(f"execution_noop:{ex.confirm_week}" for ex, noop in executed if noop)
                     + tuple(point.flags))
        self.observed += 1
        self.last_key = point.week_key
        ms = self.machine
        return SignalRecord(
            asset=p.asset, week_key=point.week_key, available_at=point.available_at,
            price=point.price, sma=s, lower_band=lower, upper_band=upper, condition=cond,
            exit_counter=ms.exit_counter, entry_counter=ms.entry_counter,
            confirmed_state=ms.state, state_basis=ms.basis, confirmation=confirmed is not None,
            scheduled_execution_week=scheduled, effective_state=self.queue.effective_state,
            executed_target=exec_target, flags=all_flags)


def evaluate(series, params: SignalParams, until=None) -> tuple:
    """Signal-only pipeline over ``series`` (optionally only weeks < ``until``).
    Returns (records, machine_state, queue)."""
    tracker = SignalTracker(params)
    records = []
    for p in series.points:
        if until is not None and p.week_key >= until:
            break
        executed = tracker.due(p.week_key)
        records.append(tracker.observe(p, executed))
    return tuple(records), tracker.machine, tracker.queue


@dataclass(frozen=True)
class PreStartState:
    """Signal state at the start of the first backtest week (SIG-003, SIG-018, Q-019)."""
    asset: str
    first_week: dt.date
    history_weeks: int
    confirmed_state: State
    state_basis: str
    exit_counter: int
    entry_counter: int
    effective_state: State
    pending: tuple            # ScheduledExecution with execution_week >= first_week
    last_history_key: object
    issues: tuple = ()


# Pre-start reconstructions are pure functions of (price points, parameters, first week); every
# grid point / profile / shadow run on one prepared input repeats the same one. The memo keeps
# the reconstructed tracker and hands out deep copies, so no run shares mutable state and the
# result is identical to replaying the history. The key holds the (immutable) points tuple
# itself, so a different series can never hit another series' entry.
_RECONSTRUCTION_MEMO: "OrderedDict" = OrderedDict()
_MEMO_SIZE = 64


def reconstruct_tracker(series, params: SignalParams, first_week: dt.date):
    """Rebuild the tracker on the whole history before ``first_week`` (Q-012 point 3).
    Executions scheduled before ``first_week`` only set the effective state (no trades,
    SIG-019); later ones stay pending and become in-backtest trades (Q-019).
    Returns (tracker, PreStartState); the tracker is always a private object."""
    points = series.points
    key = (id(points), params, first_week)
    hit = _RECONSTRUCTION_MEMO.get(key)
    if hit is not None and hit[0] is points:
        _RECONSTRUCTION_MEMO.move_to_end(key)
        return copy.deepcopy(hit[1]), hit[2]
    tracker, state = _reconstruct_tracker(points, params, first_week)
    _RECONSTRUCTION_MEMO[key] = (points, copy.deepcopy(tracker), state)
    while len(_RECONSTRUCTION_MEMO) > _MEMO_SIZE:
        _RECONSTRUCTION_MEMO.popitem(last=False)
    return tracker, state


def _reconstruct_tracker(points, params: SignalParams, first_week: dt.date):
    tracker = SignalTracker(params)
    for p in points:
        if p.week_key >= first_week:
            break
        tracker.observe(p, tracker.due(p.week_key), record=False)
    tracker.due(first_week - dt.timedelta(days=1))
    issues = []
    ms = tracker.machine
    if ms.basis == "fallback":
        issues.append(ValidationIssue(
            Severity.WARNING, "initial_state_fallback", params.asset,
            f"no confirmed transition in {tracker.observed} weeks of history before {first_week}; "
            "state RISK_ON is a fallback (SIG-003)", first_week, "SIG-003"))
    state = PreStartState(params.asset, first_week, tracker.observed, ms.state, ms.basis,
                          ms.exit_counter, ms.entry_counter, tracker.effective_state,
                          tracker.queue.pending, tracker.last_key, tuple(issues))
    return tracker, state


def reconstruct(series, params: SignalParams, first_week: dt.date) -> PreStartState:
    return reconstruct_tracker(series, params, first_week)[1]
