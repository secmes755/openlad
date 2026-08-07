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
| B1 | F401 unused imports (~52) | ✅ done (2026-08-07) |
| B2 | F841 unused variables (~16) | ✅ done (2026-08-07) |
| B3 | F541 / F811 / E722 (~30) | ✅ done (2026-08-07) |
| B4 | UP006/UP045 modern type annotations (~624, sub-batched) | ✅ done (2026-08-07, auto-fix) |
| B5 | W293/W291 whitespace (~512, auto-fix) | ✅ done (2026-08-07, auto-fix) |
| B6 | I001 import sorting (~57, auto-fix) | ✅ done (2026-08-07, auto-fix) |
| B7 | UP035 / E402 / E741 remaining (~110) | ✅ done (2026-08-07); 17 E402 kept in baseline by design (sys.path hacks) |

Note: all batches were completed in one pass with `ruff --fix` (+ safe manual
cleanup of E741/E722/F401/UP035); the remaining baseline is 17 E402 entries.
Regenerate baseline (repo-relative paths so the snapshot is machine-independent):
```bash
ruff check core --output-format=json 2>/dev/null | \
  python3 -c "import json,os,sys; d=json.load(sys.stdin); [x.update(filename=os.path.relpath(x['filename'], os.getcwd())) for x in d]; json.dump(d, sys.stdout)" > .ruff-baseline.json
```
