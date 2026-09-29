# Handoff prompt for the second model

Implement an independent V2 reference backtester from this clean-room package.

Read, in order:
1. `CLEANROOM_POLICY.md`
2. `AGENTS.md`
3. `input/python_backtest_specification_v3_1.csv`
4. `DATA_MAP.csv`
5. `ARCHITECTURE_GUIDE.md`

Do not access anything outside this package and do not seek the existing V1 implementation or its outputs.

Start by running:

```bash
python tools/verify_inputs.py
python tools/init_compliance.py
```

Then audit all input schemas against the specification and create `work/implementation_questions.csv` before writing the core engine.

Implement all `MUST` requirements and derive tests independently from the specification, including TEST-001 through TEST-054. Keep every requirement traceable in `work/compliance_matrix.csv`.

Do not optimize the implementation to agree with any unknown reference output. Correctness is determined by the specification.

When the implementation and tests are complete, set every satisfied `MUST` row in the compliance matrix to `PASS`, run the full test suite, and run:

```bash
python tools/freeze_v2.py
```

Stop after producing `V2_FREEZE_MANIFEST.json`. Do not perform A/B comparison with V1 in this phase.
