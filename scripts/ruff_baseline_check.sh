#!/usr/bin/env bash
# Baseline check for core/: tolerate the recorded existing violations, but
# fail on any NEW violation (per file + rule code, count-based so line-number
# drift does not cause false positives).
#
# Usage:
#   ./scripts/ruff_baseline_check.sh [path]      # default: core
#
# Regenerate the baseline snapshot after intentionally cleaning up violations:
#   ruff check core --output-format=json > .ruff-baseline.json
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-core}"
BASELINE_FILE=".ruff-baseline.json"
if [ ! -f "$BASELINE_FILE" ]; then
  echo "error: $BASELINE_FILE not found" >&2
  exit 1
fi

# current violations as JSON (ruff exits 1 when violations exist, but still
# prints the full JSON to stdout — so do NOT append a fallback value here)
CURRENT=$(ruff check "$TARGET" --output-format=json 2>/dev/null || true)
TMP_JSON=$(mktemp)
trap 'rm -f "$TMP_JSON"' EXIT
echo "$CURRENT" > "$TMP_JSON"

python3 - "$BASELINE_FILE" "$TMP_JSON" <<'PY'
import collections, json, sys

baseline_path, current_path = sys.argv[1], sys.argv[2]
current = json.load(open(current_path))

def key(v):
    return (v.get("filename", ""), v.get("code", ""))

base_counts = collections.Counter(key(v) for v in json.load(open(baseline_path)))
cur_counts = collections.Counter(key(v) for v in current)

new_total = 0
for k, c in cur_counts.items():
    if c > base_counts.get(k, 0):
        new_total += c - base_counts.get(k, 0)
        print(f"  {k[0]}: {k[1]} x{c} (baseline {base_counts.get(k, 0)})")

if new_total:
    print(f"FAIL: {new_total} new violation(s) beyond baseline")
    sys.exit(1)
print("baseline check ok")
PY
