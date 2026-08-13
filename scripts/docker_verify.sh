#!/usr/bin/env bash
# Docker deployment verification for OpenLAD 0.4.0.
#
# Verifies the single-container deployment against the real stack:
#   - image build (--no-cache)
#   - container health (JSON status field, not HTTP code)
#   - first-startup admin bootstrap via OPENLAD_ADMIN_PASSWORD
#   - authenticated API access
#   - real document ingestion + query (validates host-network path to the
#     model services)
#   - persistence across container restart
#   - negative case: unreachable model endpoint -> degraded health + warning
#
# Prerequisites:
#   - docker installed, user in the docker group
#   - model services running on the HOST (llama-server on :8080 / :8081)
#   - port 11296 free on the host (stop any local uvicorn first; the
#     container binds the host network)
#
# Usage:
#   OPENLAD_ADMIN_PASSWORD=... ./scripts/docker_verify.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${OPENLAD_VERIFY_IMAGE:-openlad:0.4.0-verify}"
NAME="openlad_verify"
DATA_DIR="/tmp/openlad_docker_verify_data"
TEST_DOC="${TEST_DOC:-$REPO_ROOT/fixtures/Rockchip-RK3588-Datasheet-V1.9.pdf}"
ADMIN_PASS="${OPENLAD_ADMIN_PASSWORD:-VerifyPass-2026}"
API="http://127.0.0.1:11296"
LLM_URL="${OPENLAD_LLM_URL:-http://127.0.0.1:8080/v1}"
EMB_URL="${OPENLAD_EMB_URL:-http://127.0.0.1:8081/v1}"
PASS=0
FAIL=0

ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker rm -f "${NAME}_bad" >/dev/null 2>&1 || true
  # Files written by the container are root-owned; best-effort only.
  rm -rf "$DATA_DIR" "$DATA_DIR"_bad 2>/dev/null || true
}
trap cleanup EXIT
cleanup
mkdir -p "$DATA_DIR" "$DATA_DIR"_bad

# Pre-flight: model services must be reachable from the host.
echo "[preflight] LLM $LLM_URL / EMB $EMB_URL"
curl -fsS -m 5 "$LLM_URL/models" >/dev/null 2>&1 || { echo "LLM endpoint unreachable: $LLM_URL"; exit 2; }
curl -fsS -m 5 "$EMB_URL/models" >/dev/null 2>&1 || { echo "EMB endpoint unreachable: $EMB_URL"; exit 2; }
if curl -fsS -m 3 "$API/api/v1/health" >/dev/null 2>&1; then
  echo "ERROR: something already listens on 11296 (host-network container would clash). Stop it first." >&2
  exit 2
fi

echo ""
echo "== T1: docker build =="
NO_CACHE_FLAG="${NO_CACHE:---no-cache}"
if sg docker -c "docker build $NO_CACHE_FLAG --build-arg PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/} -t $IMAGE ." >/tmp/openlad_docker_build.log 2>&1; then
  ok "build succeeded"
else
  bad "build failed (see /tmp/openlad_docker_build.log)"
  tail -20 /tmp/openlad_docker_build.log
  exit 1
fi

echo ""
echo "== T2: run + health (JSON status == ok) =="
docker run -d --name "$NAME" --network host \
  -e OPENLAD_LLM_URL="$LLM_URL" \
  -e OPENLAD_EMB_URL="$EMB_URL" \
  -e OPENLAD_LLM_MODEL="${OPENLAD_LLM_MODEL:-qwen3.5-9b}" \
  -e OPENLAD_EMB_MODEL="${OPENLAD_EMB_MODEL:-qwen3-embedding-0.6b}" \
  -e OPENLAD_DATA_DIR=/app/data \
  -e OPENLAD_ADMIN_PASSWORD="$ADMIN_PASS" \
  -v "$DATA_DIR:/app/data" \
  "$IMAGE" >/dev/null

HEALTH=""
for i in $(seq 1 60); do
  HEALTH=$(curl -fsS -m 3 "$API/api/v1/health" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
  [ "$HEALTH" = "ok" ] && break
  sleep 2
done
if [ "$HEALTH" = "ok" ]; then ok "health status=ok"; else bad "health status='$HEALTH' (want ok)"; fi

echo ""
echo "== T3: first-startup admin bootstrap =="
LOGIN=$(curl -fsS -m 10 -X POST "$API/api/v1/login" -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASS\"}" 2>/dev/null || true)
KEY=$(echo "$LOGIN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null || true)
if [ -n "$KEY" ]; then ok "admin login issued api_key (${KEY:0:8}...)"; else bad "admin login failed: $LOGIN"; fi

echo ""
echo "== T4: authenticated API access =="
CODE=$(curl -sS -m 10 -o /tmp/verify_docs.json -w "%{http_code}" \
  -H "Authorization: Bearer $KEY" "$API/api/v1/documents")
if [ "$CODE" = "200" ]; then ok "GET /api/v1/documents -> 200"; else bad "GET documents -> $CODE"; fi
CODE=$(curl -sS -m 10 -o /dev/null -w "%{http_code}" "$API/api/v1/documents")
if [ "$CODE" = "401" ]; then ok "no-key GET documents -> 401"; else bad "no-key GET documents -> $CODE (want 401)"; fi

echo ""
echo "== T5: real document ingestion + query (host-network model path) =="
if [ ! -f "$TEST_DOC" ]; then
  bad "test doc missing: $TEST_DOC (set TEST_DOC=...)"
else
  UPLOAD=$(curl -fsS -m 120 -H "Authorization: Bearer $KEY" \
    -F "file=@$TEST_DOC" "$API/api/v1/documents/upload" 2>/dev/null || true)
  TASK=$(echo "$UPLOAD" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null || true)
  if [ -z "$TASK" ]; then
    bad "upload failed: $UPLOAD"
  else
    ok "upload accepted (task ${TASK:0:8})"
    DONE=""
    for i in $(seq 1 120); do
      ST=$(curl -fsS -m 10 -H "Connection: close" -H "Authorization: Bearer $KEY" \
        "$API/api/v1/documents/upload-progress/$TASK" 2>/dev/null | \
        python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
      [ "$ST" = "completed" ] && { DONE=1; break; }
      sleep 5
    done
    if [ -n "$DONE" ]; then
      ok "ingestion completed"
      ANS=$(curl -fsS -m 180 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
        -d '{"query":"RK3588 datasheet 中，CPU 的最大频率是多少？"}' "$API/api/v1/query" 2>/dev/null | \
        python3 -c "import json,sys; print(json.load(sys.stdin).get('answer',''))" 2>/dev/null || true)
      if [ -n "$ANS" ]; then ok "query answered (${ANS:0:80}...)"; else bad "query returned empty answer"; fi
    else
      bad "ingestion timed out"
    fi
  fi
fi

echo ""
echo "== T6: persistence across restart =="
DOCS_BEFORE=$(curl -fsS -m 10 -H "Authorization: Bearer $KEY" "$API/api/v1/documents" 2>/dev/null | \
  python3 -c "import json,sys; print(len(json.load(sys.stdin).get('documents',[])))" 2>/dev/null || echo "?")
docker restart "$NAME" >/dev/null
sleep 8
HEALTH2=""
for i in $(seq 1 30); do
  HEALTH2=$(curl -fsS -m 3 "$API/api/v1/health" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
  [ "$HEALTH2" = "ok" ] && break
  sleep 2
done
DOCS_AFTER=$(curl -fsS -m 10 -H "Authorization: Bearer $KEY" "$API/api/v1/documents" 2>/dev/null | \
  python3 -c "import json,sys; print(len(json.load(sys.stdin).get('documents',[])))" 2>/dev/null || echo "?")
if [ "$HEALTH2" = "ok" ] && [ "$DOCS_BEFORE" = "$DOCS_AFTER" ] && [ "$DOCS_AFTER" != "0" ] && [ "$DOCS_AFTER" != "?" ]; then
  ok "restart healthy, docs persisted ($DOCS_AFTER)"
else
  bad "restart: health=$HEALTH2 docs $DOCS_BEFORE -> $DOCS_AFTER"
fi

echo ""
echo "== T7: negative case — unreachable model endpoint =="
# Stop the main container first: both containers use host networking and
# would clash on port 11296.
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "${NAME}_bad" --network host \
  -e OPENLAD_LLM_URL="http://127.0.0.1:19999/v1" \
  -e OPENLAD_EMB_URL="$EMB_URL" \
  -e OPENLAD_DATA_DIR=/app/data \
  -e OPENLAD_ADMIN_PASSWORD="$ADMIN_PASS" \
  -v "$DATA_DIR"_bad:/app/data \
  "$IMAGE" >/dev/null
sleep 12
BAD_LOGS=$(docker logs "${NAME}_bad" 2>&1 | grep -c "WARNING: a model endpoint is unreachable" || true)
BAD_HEALTH=$(curl -fsS -m 5 "$API/api/v1/health" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "?")
if [ "$BAD_LOGS" -ge 1 ] && [ "$BAD_HEALTH" = "degraded" ]; then
  ok "unreachable endpoint -> degraded + actionable warning"
else
  bad "negative case: warning=$BAD_LOGS health=$BAD_HEALTH (want >=1 and degraded)"
fi

echo ""
echo "=========================================="
echo " docker verification: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
