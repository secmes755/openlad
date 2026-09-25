"""The streaming query endpoint must be rate limited like /query.

/api/v1/query/stream runs the same heavy pipeline as /query (shared
pre-flight, same global query lock), but the path list in
RateLimitMiddleware._get_limit_key only named /api/v1/query, so the
stream variant was unmetered: a caller throttled to a few queries per
minute could hit /query/stream at any frequency and queue unboundedly
on the global lock.

Pinned behavior: /query/stream draws from the same per-caller "query"
bucket as /query — same pipeline, same budget.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.middleware.rate_limit import RateLimitMiddleware

_HEADERS = {"Authorization": "Bearer caller-key-1"}


def _client(monkeypatch, query_limit: int = 2) -> TestClient:
    monkeypatch.setattr(RateLimitMiddleware, "_get_limits", lambda self: (query_limit, 1))
    app = FastAPI()

    @app.post("/api/v1/query")
    def query():
        return {"ok": True}

    @app.post("/api/v1/query/stream")
    def query_stream():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware)
    return TestClient(app)


def test_query_stream_is_rate_limited(monkeypatch):
    client = _client(monkeypatch)

    assert client.post("/api/v1/query/stream", headers=_HEADERS).status_code == 200
    assert client.post("/api/v1/query/stream", headers=_HEADERS).status_code == 200
    assert client.post("/api/v1/query/stream", headers=_HEADERS).status_code == 429, \
        "the streaming endpoint escaped the query rate limit"


def test_query_stream_shares_the_query_bucket(monkeypatch):
    """Same pipeline → same budget: /query and /query/stream draw from one
    per-caller bucket, not two independent quotas."""
    client = _client(monkeypatch, query_limit=2)

    assert client.post("/api/v1/query", headers=_HEADERS).status_code == 200
    assert client.post("/api/v1/query/stream", headers=_HEADERS).status_code == 200
    assert client.post("/api/v1/query", headers=_HEADERS).status_code == 429, \
        "stream calls did not consume the shared query quota"
