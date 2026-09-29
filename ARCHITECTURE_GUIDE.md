# Architecture guide — V2 reference engine

This guide is non-normative. The specification remains authoritative.

## Recommended layers

- `config.py`: defaults, config loading, CLI precedence
- `models.py`: typed state/event structures
- `data_loader.py`: source-specific parsing
- `calendar.py`: week keys and information availability
- `validation.py`: schemas, gaps, duplicates, numeric checks
- `signals.py`: SMA, thresholds, hysteresis, confirmation, delay
- `allocation.py`: strategic sleeves and signal-reserve split
- `ledger.py`: holdings, FIFO lots, realized gains
- `tax.py`: tax policies and annual state
- `rebalancing.py`: calendar/band rebalance and sell-to-pay
- `portfolio.py`: portfolio state transitions
- `engine.py`: deterministic weekly pipeline
- `metrics.py`: metric conventions from the specification
- `optimizer.py`: scans/grid/walk-forward orchestration
- `reporting.py`: required audit files

## Time model

Prefer explicit separation of:
- `week_key`: economic/calendar week identifier
- `available_at`: when information becomes usable
- `execution_week`: when a scheduled action may execute

This makes BTC availability, confirmation, delay, and no-look-ahead auditable.

## Central weekly transition

Prefer one core transition used by `run`, scans, optimization, tax comparison, and walk-forward.

Conceptually:

1. execute previously scheduled signal trades
2. determine amounts due
3. strategic rebalance or deterministic sell-to-pay
4. apply weekly returns
5. charge immediate taxes
6. evaluate end-of-week signals and schedule future execution

Do not duplicate portfolio logic inside optimizer/scanner commands.

## Audit trail

Every transaction should carry a reason such as:
- `signal_exit`
- `signal_reentry`
- `calendar_rebalance`
- `band_rebalance`
- `sell_to_pay`
- `walk_forward_rebalance`
- `terminal_liquidation`

Every tax/cost event should carry an explicit event type and period.

## A/B readiness

Design outputs so a later comparator can locate the first divergence at these layers:

normalized data -> signals -> scheduled executions -> trades -> taxes -> holdings -> weekly NAV -> metrics
