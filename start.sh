#!/bin/bash
# =============================================================================
# OpenLAD External Mode startup script
#
# This script only starts the OpenLAD API service and does NOT manage
# llama-server processes. You must deploy LLM / Embedding backends
# (llama-server / vLLM / Ollama) separately, and point to them via
# OPENLAD_LLM_URL / OPENLAD_EMB_URL.
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Environment variables (can be overridden in .env) ──
# Model service URLs (default to common ports)
: ${OPENLAD_LLM_URL:="http://127.0.0.1:8080/v1"}
: ${OPENLAD_EMB_URL:="http://127.0.0.1:8081/v1"}
: ${OPENLAD_CHART_VLM_URL:="$OPENLAD_LLM_URL"}

# Model names (matching backend registered model names)
: ${OPENLAD_LLM_MODEL:="qwen3.5-9b"}
: ${OPENLAD_EMB_MODEL:="qwen3-embedding-0.6b"}
: ${OPENLAD_CHART_VLM_MODEL:="$OPENLAD_LLM_MODEL"}

# MMProj path for vision capabilities (auto-detect if not set)
: ${OPENLAD_LLM_MMPROJ_PATH:=""}

# API service
: ${OPENLAD_HOST:="0.0.0.0"}
: ${OPENLAD_PORT:="11296"}
OPENLAD_API_HOST="${OPENLAD_API_HOST:-$OPENLAD_HOST}"
OPENLAD_API_PORT="${OPENLAD_API_PORT:-$OPENLAD_PORT}"
: ${OPENLAD_DATA_DIR:="$SCRIPT_DIR/data"}

# Concurrency policy (External mode defaults to serial)
: ${OPENLAD_QUERY_CONCURRENCY_MODE:="serial"}
: ${OPENLAD_QUERY_MAX_CONCURRENT:="1"}

# Export environment variables
export OPENLAD_LLM_URL OPENLAD_EMB_URL OPENLAD_CHART_VLM_URL
export OPENLAD_LLM_MODEL OPENLAD_EMB_MODEL OPENLAD_CHART_VLM_MODEL
export OPENLAD_LLM_MMPROJ_PATH
export OPENLAD_HOST OPENLAD_PORT OPENLAD_DATA_DIR
export OPENLAD_API_HOST OPENLAD_API_PORT
export OPENLAD_QUERY_CONCURRENCY_MODE OPENLAD_QUERY_MAX_CONCURRENT

echo "=============================================="
echo " OpenLAD (External Mode)"
echo "=============================================="
echo " LLM backend:   $OPENLAD_LLM_URL"
echo " Embedding:     $OPENLAD_EMB_URL"
echo " API listening: http://$OPENLAD_HOST:$OPENLAD_PORT"
echo " Data dir:      $OPENLAD_DATA_DIR"
echo " Concurrency:   $OPENLAD_QUERY_CONCURRENCY_MODE (max $OPENLAD_QUERY_MAX_CONCURRENT)"
echo "=============================================="
echo ""
echo "⚠  Make sure LLM / Embedding services are running,"
echo "   otherwise OpenLAD will start but queries will fail."
echo ""

# Detect Python interpreter: prefer .venv for local dev, fall back to system python3
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
    echo "Using venv Python: $PYTHON_BIN"
else
    PYTHON_BIN="python3"
    echo "Using system Python: $PYTHON_BIN"
fi

# Start API
exec "$PYTHON_BIN" -m uvicorn core.api.main:app \
    --host "$OPENLAD_HOST" \
    --port "$OPENLAD_PORT" \
    --log-level info
