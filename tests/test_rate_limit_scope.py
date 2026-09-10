"""Rate limiting is scoped to the caller, not to a context that does not exist yet.

RateLimitMiddleware runs outside TenantMiddleware, so a flood is rejected before
any authentication work. But ``_get_limit_key`` read the tenant from the tenant
contextvar that TenantMiddleware sets — and that context is empty at this layer,
so the tenant_id was always "unknown" and every caller shared one bucket: a single
busy client could exhaust the quota of all the others.

The key is now derived from the presented credential (hashed, never stored or
logged verbatim), falling back to the client IP when no credential is present.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.middleware.rate_limit import RateLimitMiddleware


def _client(monkeypatch, query_limit: int = 2) -> TestClient:
    monkeypatch.setattr(RateLimitMiddleware, "_get_limits", lambda self: (query_limit, 1))
    app = FastAPI()

    @app.post("/api/v1/query")
    def query():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware)
    return TestClient(app)


def _post(client: TestClient, api_key: str | None = None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return client.post("/api/v1/query", headers=headers)


def test_limit_is_scoped_per_credential(monkeypatch):
    client = _client(monkeypatch)

    assert _post(client, "key-aaa").status_code == 200
    assert _post(client, "key-aaa").status_code == 200
    assert _post(client, "key-aaa").status_code == 429, "quota not enforced for the caller"

    # A different credential must be unaffected. Before the fix every caller fell
    # into the shared "unknown" bucket, so this returned 429 as well.
    assert _post(client, "key-bbb").status_code == 200


def test_without_a_credential_the_client_ip_is_the_bucket(monkeypatch):
    client = _client(monkeypatch)

    assert _post(client).status_code == 200
    assert _post(client).status_code == 200
    assert _post(client).status_code == 429


def test_the_api_key_is_never_logged_verbatim(monkeypatch, caplog):
    client = _client(monkeypatch)

    with caplog.at_level("WARNING"):
        for _ in range(3):
            _post(client, "super-secret-token")

    assert any("Rate limit hit" in record.message for record in caplog.records), \
        "expected the throttle to fire"
    assert "super-secret-token" not in caplog.text, "the credential leaked into the logs"
