#!/usr/bin/env bash
# OpenLAD local smoke check (run against a locally running instance).
# Verifies: health -> upload (idempotent) -> ingestion -> question -> non-empty answer.
#
# Usage:
#   ./scripts/smoke.sh                    # uses fixtures/Rockchip-RK3588-Datasheet-V1.9.pdf
#   ./scripts/smoke.sh path/to/file.pdf
#
# Requires:
#   - OpenLAD running on $OPENLAD_BASE_URL (default http://127.0.0.1:11296)
#   - OPENLAD_API_KEY, or the key cached at /tmp/openlad_api_key
#   - Optional OPENLAD_TENANT; when unset the API key's own tenant is used
set -euo pipefail

BASE_URL="${OPENLAD_BASE_URL:-http://127.0.0.1:11296}"
KEY="${OPENLAD_API_KEY:-$(cat /tmp/openlad_api_key 2>/dev/null || true)}"
if [ -z "$KEY" ]; then
  echo "error: OPENLAD_API_KEY is required (or /tmp/openlad_api_key)" >&2
  exit 1
fi
DOC="${1:-fixtures/Rockchip-RK3588-Datasheet-V1.9.pdf}"
if [ ! -f "$DOC" ]; then
  echo "error: document not found: $DOC" >&2
  exit 1
fi

# Tenant: explicit OPENLAD_TENANT wins; otherwise omit X-Tenant-ID so the
# server auto-associates the API key's own tenant.
AUTH=(-H "Authorization: Bearer $KEY")
if [ -n "${OPENLAD_TENANT:-}" ]; then
  AUTH+=(-H "X-Tenant-ID: $OPENLAD_TENANT")
fi
DOCNAME="$(basename "$DOC")"

echo "[1/4] health ..."
curl -fsS -m 10 "${AUTH[@]}" "$BASE_URL/api/v1/health" >/dev/null

echo "[2/4] check existing / upload $DOCNAME ..."
EXISTING=$(curl -fsS -m 30 "${AUTH[@]}" "$BASE_URL/api/v1/documents" | \
  python3 -c "import json,sys; docs=json.load(sys.stdin).get('documents',[]); print(next((d['id'] for d in docs if d.get('filename','').endswith('$DOCNAME')), ''))")
if [ -n "$EXISTING" ]; then
  echo "  already ingested (id=${EXISTING:0:8}), skipping upload"
else
  UPLOAD=$(curl -fsS -m 60 "${AUTH[@]}" -F "file=@$DOC" \
    "$BASE_URL/api/v1/documents/upload")
  TASK=$(echo "$UPLOAD" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))")
  if [ -z "$TASK" ]; then
    echo "error: no task_id in upload response: $UPLOAD" >&2
    exit 1
  fi
  echo "[3/4] wait for ingestion ..."
  for i in $(seq 1 180); do
    STATUS=$(curl -fsS -m 10 -H "Connection: close" "${AUTH[@]}" \
      "$BASE_URL/api/v1/documents/upload-progress/$TASK" | \
      python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" || echo "")
    if [ "$STATUS" = "completed" ]; then break; fi
    [ "$i" = 180 ] && { echo "error: ingestion timeout" >&2; exit 1; }
    sleep 5
  done
fi

echo "[4/4] ask a question ..."
ANSWER=$(curl -fsS -m 120 "${AUTH[@]}" -H "Content-Type: application/json" \
  -d '{"query":"What chip model is described in the Rockchip RK3588 Datasheet document?","industry":"auto"}' \
  "$BASE_URL/api/v1/query" | python3 -c "import json,sys; print(json.load(sys.stdin).get('answer',''))")
if [ -z "$ANSWER" ]; then
  echo "error: empty answer" >&2
  exit 1
fi
echo "answer: ${ANSWER:0:150}..."
echo "smoke ok"
