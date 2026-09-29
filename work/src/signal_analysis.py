"""Signal pipeline: indicators -> confirmation -> scheduling -> effective state, historical
reconstruction before a start week (SIG-003, SIG-016, SIG-018, SIG-019, NORM-010/012/016).

Per weekly observation K of an asset (ascending):
  1. start of week K: executions with execution_week <= K are applied (effective state);
  2. end of week K (evaluation time, Sunday): the observation must already be available
     (BTC close_date == Sunday); SMA, bands, condition and counters are updated; a confirmed
     transition schedules one execution at K + delay.
No portfolio, trade or tax exists here; reconstruction before the start only rebuilds state.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .availability import assert_available, evaluation_time
from .confirmation import MachineState, step
from .models import Severity, SignalParams, SignalRecord, State, ValidationIssue
from .scheduling import ExecutionQueue
from .signals import bands, classify, sma


def evaluate(series, params: SignalParams, until=None) -> tuple:
    """Run the full signal pipeline over ``series`` (optionally only weeks < ``until``).
    Returns (records, machine_state, queue)."""
    points = [p for p in series.points if until is None or p.week_key < until]
    smas = sma([p.price for p in points], params.ma)
    ms = MachineState()
    queue = ExecutionQueue(params.asset, State.RISK_ON)
    records = []
    for p, s in zip(points, smas):
        executed = queue.due(p.week_key)
        exec_target = next((ex.target_state for ex, noop in reversed(executed) if not noop), None)
        assert_available(p, evaluation_time(p.week_key), f"{params.asset}: ")
        lower, upper = bands(s, params.threshold_off, params.threshold_on)
        cond = classify(p.price, lower, upper)
        ms, confirmed, flags = step(ms, p.week_key, cond, params)
        scheduled = None
        if confirmed is not None:
            scheduled = queue.schedule(p.week_key, params.delay, confirmed).execution_week
        flags = flags + tuple(f"execution_noop:{ex.confirm_week}" for ex, noop in executed if noop)
        flags = flags + tuple(f for f in p.flags)
        records.append(SignalRecord(
            asset=params.asset, week_key=p.week_key, available_at=p.available_at, price=p.price,
            sma=s, lower_band=lower, upper_band=upper, condition=cond,
            exit_counter=ms.exit_counter, entry_counter=ms.entry_counter,
            confirmed_state=ms.state, state_basis=ms.basis, confirmation=confirmed is not None,
            scheduled_execution_week=scheduled, effective_state=queue.effective_state,
            executed_target=exec_target, flags=flags))
    return tuple(records), ms, queue


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


def reconstruct(series, params: SignalParams, first_week: dt.date) -> PreStartState:
    """Rebuild SMA/counters/state on the whole history before ``first_week`` (Q-012 point 3).
    Executions scheduled before ``first_week`` define the effective state used for the
    initial sleeve split; later ones stay pending and become in-backtest trades."""
    records, ms, queue = evaluate(series, params, until=first_week)
    queue.due(first_week - dt.timedelta(days=1))
    issues = []
    if ms.basis == "fallback":
        issues.append(ValidationIssue(
            Severity.WARNING, "initial_state_fallback", params.asset,
            f"no confirmed transition in {len(records)} weeks of history before {first_week}; "
            "state RISK_ON is a fallback (SIG-003)", first_week, "SIG-003"))
    return PreStartState(params.asset, first_week, len(records), ms.state, ms.basis,
                         ms.exit_counter, ms.entry_counter, queue.effective_state, queue.pending,
                         records[-1].week_key if records else None, tuple(issues))
