#!/bin/bash
# OpenLAD container entrypoint.
#
# Starts the API, then verifies the configured model endpoints are reachable
# and prints actionable guidance when they are not (the classic pitfall: the
# container's 127.0.0.1 is the container itself, not the host).
set -euo pipefail

# Start the API (CMD args) in the background so we can self-check below.
"$@" &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' TERM INT

# Wait for the API to accept requests.
for _ in $(seq 1 60); do
    if curl -fsS -m 3 http://127.0.0.1:11296/api/v1/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

python3 - "${OPENLAD_LLM_URL:-}" "${OPENLAD_EMB_URL:-}" <<'PY'
import sys
import urllib.request

llm, emb = sys.argv[1], sys.argv[2]

def probe(url: str) -> str:
    if not url:
        return "not-configured"
    try:
        with urllib.request.urlopen(f"{url}/models", timeout=5) as r:
            return "ok" if r.status in (200, 401, 404) else f"degraded({r.status})"
    except Exception as e:
        return f"unreachable({type(e).__name__})"

ls, es = probe(llm), probe(emb)
print(f"[openlad] model endpoints: LLM {ls} ({llm}) | EMB {es} ({emb})")

if ls.startswith("unreachable") or es.startswith("unreachable"):
    print("[openlad] WARNING: a model endpoint is unreachable.")
    print("  If your model service runs on THIS host (localhost), the container")
    print("  cannot reach it via 127.0.0.1 -- that address is the container itself.")
    print("  Options:")
    print("    - run with --network host (docker-compose default), keep 127.0.0.1 URLs")
    print("    - bridge network: use http://host.docker.internal:PORT/v1 plus")
    print("      --add-host=host.docker.internal:host-gateway")
    print("    - cloud endpoints: use the public URL directly")
PY

wait "$API_PID"
