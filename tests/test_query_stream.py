"""SSE contract for POST /query/stream.

Contract under test (with a stub engine, no real LLM):
- progress events (planning/retrieving/generating) arrive in order
- the terminal frame is {"stage": "result", "result": {...}} whose payload
  matches what /query returns (answer/sources/session_id fields)
- the stub engine's progress_cb is invoked from a worker thread, proving the
  thread-safe hop into the event loop works
"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from core.api.main import app
from core.db.system_db import get_system_db
from core.tenant.auth import get_auth_manager
from core.tenant.tenant_manager import get_tenant_manager

client = TestClient(app)


class StubEngine:
    def __init__(self):
        self.cb_threads = []

    def query(self, query_text, tenant_id, industry_hint=None, chat_history=None, progress_cb=None):
        if progress_cb:
            self.cb_threads.append(threading.current_thread().name)
            progress_cb("planning")
            progress_cb("retrieving")
            progress_cb("generating")
        return {
            "answer": "stub answer",
            "sources": [{"page": 1, "doc_id": "docA", "preview": "p"}],
            "confidence": "high",
        }


@pytest.fixture(scope="module")
def seeds():
    db = get_system_db()
    db.init_schema()
    tm = get_tenant_manager()
    if not tm.get_tenant("streamt"):
        tm.create_tenant(name="tenant streamt", description="",
                         storage_quota_mb=64, tenant_id="streamt")
    auth = get_auth_manager()
    existing = [u for u in auth.list_users("streamt") if u.username == "stream_user"]
    key = existing[0].api_key if existing else auth.create_user(
        tenant_id="streamt", username="stream_user", role="user").api_key

    engine = StubEngine()
    app.state.query_engine = engine
    yield {"key": key, "engine": engine}
    app.state.query_engine = None


def _parse_sse(body: str) -> list[dict]:
    frames = []
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            frames.append(json.loads(block[6:]))
    return frames


def test_stream_events_and_result(seeds):
    r = client.post(
        "/api/v1/query/stream",
        json={"query": "what is this?"},
        headers={"Authorization": f"Bearer {seeds['key']}"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    stages = [f["stage"] for f in frames]
    # Progress frames in order, terminated by a result frame
    assert stages[:3] == ["planning", "retrieving", "generating"], stages
    assert stages[-1] == "result"

    result = frames[-1]["result"]
    assert result["answer"] == "stub answer"
    assert result["sources"][0]["doc_id"] == "docA"
    assert result["session_id"]            # auto-created and persisted
    assert "elapsed_ms" in result

    # progress_cb must have fired from a non-event-loop thread
    assert seeds["engine"].cb_threads
    assert all("MainThread" not in t for t in seeds["engine"].cb_threads)


def test_stream_requires_auth():
    r = client.post("/api/v1/query/stream", json={"query": "hi"})
    assert r.status_code == 401
