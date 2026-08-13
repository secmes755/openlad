#!/usr/bin/env bash
# Local CI gate: reproduce the GitHub Actions CI jobs (lint + unit) in a
# minimal-dependency environment, so anything pushed to main is guaranteed
# to pass CI. Run automatically by the pre-push hook (.githooks/pre-push)
# or manually:
#
#   ./scripts/ci_gate.sh             # full gate
#   ./scripts/ci_gate.sh --refresh   # rebuild the cached venv first
#   ./scripts/ci_gate.sh --self-test # verify the gate's failure detection works
#
# Why this exists:
#   CI (ci.yml) installs ONLY: fastapi pydantic python-dotenv python-multipart
#   PyYAML bcrypt psutil requests pytest  (+ ruff==0.16.1 in the lint job).
#   The full local venv carries extra deps (Pillow/OCR/...) so a green local
#   pytest does NOT imply green CI -- a module that imports PIL at top level
#   passes locally and explodes on the runner. This gate reproduces the CI
#   dependency set, not the developer's.
#
# The minimal venv is cached under ~/.cache/openlad-ci-gate/ so repeat runs
# are fast. Override with OPENLAD_CI_GATE_DIR.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY_VERSION="3.11"                     # keep in sync with .github/workflows/ci.yml
GATE_DIR="${OPENLAD_CI_GATE_DIR:-$HOME/.cache/openlad-ci-gate}"
VENV="$GATE_DIR/venv"
UV="${UV:-uv}"

# CI unit-job dependency set -- keep in sync with .github/workflows/ci.yml
CI_DEPS=(fastapi pydantic python-dotenv python-multipart PyYAML bcrypt psutil requests pytest ruff==0.16.1)

refresh=0
self_test=0
for arg in "$@"; do
  case "$arg" in
    --refresh) refresh=1 ;;
    --self-test) self_test=1 ;;
    *)
      echo "error: unknown argument: $arg" >&2
      echo "usage: $0 [--refresh] [--self-test]" >&2
      exit 2
      ;;
  esac
done

echo "== openlad CI gate =="
mkdir -p "$GATE_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[env] creating minimal venv (python $PY_VERSION) at $VENV ..."
  "$UV" venv --python "$PY_VERSION" "$VENV"
  refresh=1
fi

if [ "$refresh" = 1 ]; then
  echo "[env] installing CI deps: ${CI_DEPS[*]}"
  "$UV" pip install --python "$VENV" "${CI_DEPS[@]}"
fi

# sanity: the cached venv must actually contain the CI deps
if ! "$VENV/bin/python" -c "import fastapi, pydantic, pytest, requests" 2>/dev/null; then
  echo "[env] cached venv missing deps; reinstalling"
  "$UV" pip install --python "$VENV" "${CI_DEPS[@]}"
fi

if [ "$self_test" = 1 ]; then
  echo "[self-test] verifying failure detection ..."
  if "$VENV/bin/python" -c "import openlad_ci_gate_nonexistent_module" 2>/dev/null; then
    echo "FAIL: self-test: unexpected import succeeded" >&2
    exit 1
  fi
  echo "  import failure detected correctly"
  echo "[self-test] ok"
  exit 0
fi

echo ""
echo "== [1/2] lint (ruff==0.16.1, matching CI) =="
# Prepend the gate venv to PATH so ruff_baseline_check.sh also uses 0.16.1.
PATH="$VENV/bin:$PATH" "$VENV/bin/ruff" check tests scripts
PATH="$VENV/bin:$PATH" ./scripts/ruff_baseline_check.sh

echo ""
echo "== [2/2] unit (minimal-dependency pytest, matching CI unit job) =="
"$VENV/bin/python" -m pytest -q

echo ""
echo "== CI gate passed: pushed commit is expected to pass GitHub Actions =="
