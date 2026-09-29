# Backtest V2 — clean-room reference implementation

This package contains only the frozen specification, whitelisted raw input data, clean-room rules, helper tools, and an empty implementation workspace.

## Start

```bash
python tools/verify_inputs.py
python tools/init_compliance.py
```

Then read `AGENTS.md` and implement only under `work/`.

## Phases

1. **Requirements audit** — map all specification rows.
2. **Input audit** — identify any schema/semantic conflict without consulting V1.
3. **Independent implementation** — implement the specification.
4. **Independent verification** — tests derived from the specification plus invariants/property tests.
5. **Freeze** — all `MUST` rows must be `PASS`; run `python tools/freeze_v2.py`.
6. **A/B comparison** — only after freeze, and outside the clean-room implementation phase.

The purpose is not to reproduce V1 by imitation. The purpose is to obtain a second implementation whose agreement or disagreement with V1 is informative.
