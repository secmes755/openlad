# Quality Cleanup Tracker

Progressive cleanup of code-quality debt. Baseline mechanics:

- `.ruff-baseline.json` records the current `core/` violations (snapshot).
- `scripts/ruff_baseline_check.sh` fails CI only on **new** violations
  (existing debt is tolerated until cleaned).
- After each batch: fix -> regenerate the baseline snapshot
  (`ruff check core --output-format=json > .ruff-baseline.json`) -> run unit
  checks -> push -> tick the box below.

| Batch | Scope | Status |
|---|---|---|
| B1 | F401 unused imports (~52) | ⬜ |
| B2 | F841 unused variables (~16) | ⬜ |
| B3 | F541 / F811 / E722 (~30) | ⬜ |
| B4 | UP006/UP045 modern type annotations (~624, sub-batched) | ⬜ |
| B5 | W293/W291 whitespace (~512, auto-fix) | ⬜ |
| B6 | I001 import sorting (~57, auto-fix) | ⬜ |
| B7 | UP035 / E402 / E741 remaining (~110) | ⬜ |

Regenerate baseline:
```bash
ruff check core --output-format=json > .ruff-baseline.json
```
