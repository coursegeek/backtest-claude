# AGENTS.md — Backtest V2 clean-room implementation

You are building an independent reference implementation of the supplied backtest specification.

## Scope

Work only inside this clean-room package.
Write implementation code only under `work/`.
Treat `input/` as immutable.

Do not inspect any V1 implementation, tests, configs, README, backlog, or generated output.

## Source of truth

`input/python_backtest_specification_v3_1.csv` is the authoritative behavioral contract.

Every row with priority `MUST` must:
- have an implementation mapping,
- have at least one independent verification/test,
- end with status `PASS` in `work/compliance_matrix.csv`.

`SHOULD` requirements may be deferred, but each deferral must be explicit.

## First actions

Before writing the core engine:

1. run `python tools/verify_inputs.py`,
2. run `python tools/init_compliance.py`,
3. audit the raw input schemas against the specification,
4. create `work/implementation_questions.csv` for any ambiguity or contradiction,
5. design module boundaries and invariants,
6. then implement.

Do not ask for V1 outputs or V1 source code.

## Required implementation properties

The implementation must be:
- deterministic,
- importable as a Python module and usable as a CLI,
- explicit about information availability and no-look-ahead behavior,
- auditable at signal, trade, tax, holdings, and NAV levels,
- able to reproduce identical output for identical resolved config and input hashes.

Keep signal generation, portfolio accounting, taxation, metrics, reporting, validation, and optimization as separable concerns.

A deterministic event/state-machine design is recommended because the specification contains confirmation, delay, information-availability dates, annual liabilities, rebalancing, sell-to-pay, and terminal settlement. This is guidance, not an excuse to change the required behavior.

## Testing

Derive tests from the specification itself, especially `TEST-001` through `TEST-054`.
Do not copy any existing project tests.

Also create independent invariant/property tests where useful, for example:
- accounting identity: assets + RF sleeves == NAV,
- no signal in T changes the return exposure already fixed for T,
- zero-cost reallocations conserve NAV,
- asset dictionary ordering does not affect results,
- parallel and serial grid search return the same ordered results,
- exact same inputs/config produce identical outputs.

## Conflicts between raw data and specification

Do not silently rename semantics or shift dates merely to make data pass.

Record:
- requirement ID,
- source file,
- observed schema/values,
- expected contract,
- chosen handling,
- confidence,
- whether the issue blocks full compliance.

## Deliverables under `work/`

At minimum:
- `backtest.py`
- `src/`
- `tests/`
- `compliance_matrix.csv`
- `implementation_questions.csv`
- `IMPLEMENTATION_NOTES.md`

The output formats required by the specification must also be implemented.

## Before comparison with V1

Run all tests and then:

```bash
python tools/freeze_v2.py
```

Do not modify V2 after looking at V1 results unless the change is documented as a post-freeze adjudication fix tied to a specification requirement.
