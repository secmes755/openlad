#!/bin/bash
# =============================================================================
# OpenLAD External Mode stop script
#
# Stops the OpenLAD API service (does NOT manage llama-server processes).
# To stop LLM / Embedding backends, handle them separately.
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${OPENLAD_PORT:-11296}"

echo "Stopping OpenLAD..."

# Method 1: Find uvicorn process by port
PID=$(lsof -ti :"$PORT" 2>/dev/null | head -1)

if [ -n "$PID" ]; then
    echo "Found process PID=$PID (port $PORT), sending SIGTERM..."
    kill "$PID" 2>/dev/null || true

    # Wait up to 10 seconds
    for i in $(seq 1 10); do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "OpenLAD stopped."
            exit 0
        fi
        sleep 1
    done

    # Force kill if still running
    echo "Process not responding, sending SIGKILL..."
    kill -9 "$PID" 2>/dev/null || true
    echo "OpenLAD force-stopped."
else
    echo "No OpenLAD process found (port $PORT not listening)."
fi
