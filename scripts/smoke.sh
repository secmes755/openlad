#!/usr/bin/env bash
# OpenLAD local verification script (run against a locally running instance).
# Reads a manifest and, for each document: idempotent upload, wait for
# ingestion, then ask each question and check its required answer terms.
#
# Usage:
#   ./scripts/smoke.sh                          # small public Markdown fixture
#   ./scripts/smoke.sh fixtures/manifest.json  # full public PDF smoke
#   ./scripts/smoke.sh path/to/manifest.json
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

# Tenant: explicit OPENLAD_TENANT wins; otherwise omit X-Tenant-ID so the
# server auto-associates the API key's own tenant.
AUTH_SCHEME="Bearer"
HEADER_FLAG=-H
AUTH=( "$HEADER_FLAG" "Authorization: ${AUTH_SCHEME} ${KEY}" )
if [ -n "${OPENLAD_TENANT:-}" ]; then
  AUTH+=(-H "X-Tenant-ID: $OPENLAD_TENANT")
fi

MANIFEST="${1:-fixtures/smoke/manifest.json}"
if [ ! -f "$MANIFEST" ]; then
  echo "error: manifest not found: $MANIFEST" >&2
  exit 1
fi

echo "[0/5] health ..."
curl -fsS -m 10 "${AUTH[@]}" "$BASE_URL/api/v1/health" >/dev/null

# Extract document file names from the manifest (newline-separated).
FILES=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print('\n'.join(x['file'] for x in d['documents']))
" "$MANIFEST")

TOTAL=$(printf '%s\n' "$FILES" | wc -l)
I=0
while IFS= read -r DOC; do
  [ -n "$DOC" ] || continue
  I=$((I + 1))
  DOCPATH="$(dirname "$MANIFEST")/$DOC"
  DOCNAME="$(basename "$DOC")"
  echo ""
  echo "[doc $I/$TOTAL] $DOCNAME"

  echo "  [upload] check existing / upload ..."
  EXISTING=$(curl -fsS -m 30 "${AUTH[@]}" "$BASE_URL/api/v1/documents" | \
    python3 -c "import json,sys; docs=json.load(sys.stdin).get('documents',[]); print(next((d['id'] for d in docs if d.get('filename','').endswith(sys.argv[1])), ''))" "$DOCNAME")
  if [ -n "$EXISTING" ]; then
    echo "  already ingested (id=${EXISTING:0:8}), skipping upload"
  else
    UPLOAD=$(curl -fsS -m 120 "${AUTH[@]}" -F "file=@$DOCPATH" \
      "$BASE_URL/api/v1/documents/upload")
    TASK=$(echo "$UPLOAD" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))")
    if [ -z "$TASK" ]; then
      echo "  error: no task_id in upload response: $UPLOAD" >&2
      exit 1
    fi
    echo "  [ingest] wait for ingestion ..."
    for i in $(seq 1 360); do
      STATUS=$(curl -fsS -m 10 -H "Connection: close" "${AUTH[@]}" \
        "$BASE_URL/api/v1/documents/upload-progress/$TASK" | \
        python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" || echo "")
      if [ "$STATUS" = "completed" ]; then break; fi
      [ "$i" = 360 ] && { echo "  error: ingestion timeout" >&2; exit 1; }
      sleep 5
    done
    echo "  ingestion completed"
  fi

  QUERIES=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
for x in d['documents']:
    if x['file'] != sys.argv[2]:
        continue
    for q in x['queries']:
        if isinstance(q, str):
            q = {'query': q, 'contains': []}
        print(json.dumps(q, ensure_ascii=False))
" "$MANIFEST" "$DOC")
  QN=0
  while IFS= read -r QUERY_JSON; do
    [ -n "$QUERY_JSON" ] || continue
    QUERY=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['query'])" "$QUERY_JSON")
    EXPECTED=$(python3 -c "import json,sys; print(json.dumps(json.loads(sys.argv[1]).get('contains', []), ensure_ascii=False))" "$QUERY_JSON")
    QN=$((QN + 1))
    echo "  [q$QN] $QUERY"
    ANSWER=$(curl -fsS -m 180 "${AUTH[@]}" -H "Content-Type: application/json" \
      -d "$(python3 -c "import json,sys; print(json.dumps({'query': sys.argv[1], 'industry': 'auto'}))" "$QUERY")" \
      "$BASE_URL/api/v1/query" | python3 -c "import json,sys; print(json.load(sys.stdin).get('answer',''))")
    if [ -z "$ANSWER" ]; then
      echo "  error: empty answer" >&2
      exit 1
    fi
    if ! printf '%s' "$ANSWER" | python3 -c "import json,sys; a=sys.stdin.read(); terms=json.loads(sys.argv[1]); ok=not a.lstrip().startswith('[System Notice:') and all(str(t).casefold() in a.casefold() for t in terms); raise SystemExit(0 if ok else 1)" "$EXPECTED"; then
      echo "  error: answer missing required terms or is a system fallback" >&2
      exit 1
    fi
    echo "  -> ${ANSWER:0:120}..."
  done <<< "$QUERIES"
done <<< "$FILES"

echo ""
echo "all documents verified ok"
