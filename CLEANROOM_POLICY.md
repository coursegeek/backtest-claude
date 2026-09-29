# Clean-room policy

## Purpose

V2 is an independent reference implementation of specification 3.1.
Its value comes from being implemented without exposure to V1 implementation choices.

## Allowed evidence

The implementer may use only:
- `input/python_backtest_specification_v3_1.csv`
- files under `input/data/`
- documents and tools supplied in this clean-room package
- standard Python/library documentation when genuinely needed

## Forbidden evidence until V2 is frozen

Do not inspect, retrieve, search, copy, import, or infer from:
- V1 `backtest.py`
- V1 `src/`
- V1 `tests/`
- V1 `configs/`
- V1 README or backlog files
- V1 generated results
- V1 expected/golden outputs
- V1 commit diffs intended to reveal implementation details

Do not navigate outside the clean-room root to look for the source repository.

## Normative priority

The specification CSV is normative.

Raw input files are evidence about available data, not permission to weaken a `MUST` requirement.
If an input file cannot satisfy a `MUST` requirement without an additional assumption:
1. document the conflict in `work/implementation_questions.csv`,
2. make the smallest explicit, auditable choice only when unambiguous,
3. otherwise fail with a clear diagnostic,
4. never consult V1 to discover what it did.

## Independence rule

Before V2 is frozen, do not compare its numerical results with V1.
Do not tune thresholds, dates, tolerances, taxes, signal timing, or parsers to make unknown V1 results match.

## Freeze gate

V2 is considered frozen only after:
- all specification `MUST` rows are mapped in `work/compliance_matrix.csv`,
- all mapped `MUST` rows have status `PASS`,
- the independent test suite passes,
- `python tools/freeze_v2.py` succeeds and creates `V2_FREEZE_MANIFEST.json`.

Only after that point may an A/B comparator see V1 outputs.
