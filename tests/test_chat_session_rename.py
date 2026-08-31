"""HTTP-layer rules for PATCH /chat/sessions/{id} (rename).

The endpoint must enforce, in order:
- authentication (401 without / with an invalid bearer key)
- input validation (400 on missing / blank title)
- ownership (404 when the session belongs to ANOTHER user — never leaks
  existence, same rule as GET/DELETE on sessions)
- happy path (200, title persisted and visible in the session list)
"""
import pytest
from fastapi.testclient import TestClient

from core.api.main import app
from core.db.system_db import get_system_db
from core.tenant.auth import get_auth_manager
from core.tenant.tenant_manager import get_tenant_manager

client = TestClient(app)


def _tenant_ready(tenant_id: str) -> None:
    tm = get_tenant_manager()
    if not tm.get_tenant(tenant_id):
        tm.create_tenant(name=f"tenant {tenant_id}", description="",
                         storage_quota_mb=64, tenant_id=tenant_id)


def _user_key(username: str, tenant_id: str) -> str:
    auth = get_auth_manager()
    existing = [u for u in auth.list_users(tenant_id) if u.username == username]
    if existing:
        return existing[0].api_key
    return auth.create_user(tenant_id=tenant_id, username=username,
                            role="user").api_key


@pytest.fixture(scope="module")
def seeds():
    db = get_system_db()
    db.init_schema()
    _tenant_ready("renamet")
    key_a = _user_key("ren_alice", "renamet")
    key_b = _user_key("ren_bob", "renamet")

    # A session owned by alice
    r = client.post("/api/v1/chat/sessions?title=alice%20chat",
                    headers={"Authorization": f"Bearer {key_a}"})
    assert r.status_code == 200, r.text
    return {"key_a": key_a, "key_b": key_b, "sid": r.json()["id"]}


def test_missing_or_invalid_token_rejected(seeds):
    r1 = client.patch(f"/api/v1/chat/sessions/{seeds['sid']}", json={"title": "x"})
    assert r1.status_code == 401
    r2 = client.patch(f"/api/v1/chat/sessions/{seeds['sid']}", json={"title": "x"},
                      headers={"Authorization": "Bearer not-a-key"})
    assert r2.status_code == 401


def test_blank_title_rejected(seeds):
    headers = {"Authorization": f"Bearer {seeds['key_a']}"}
    for body in ({}, {"title": ""}, {"title": "   "}, {"title": 123}):
        r = client.patch(f"/api/v1/chat/sessions/{seeds['sid']}", json=body, headers=headers)
        assert r.status_code == 400, body


def test_other_users_session_is_404(seeds):
    r = client.patch(f"/api/v1/chat/sessions/{seeds['sid']}", json={"title": "hijack"},
                     headers={"Authorization": f"Bearer {seeds['key_b']}"})
    assert r.status_code == 404


def test_rename_persists(seeds):
    headers = {"Authorization": f"Bearer {seeds['key_a']}"}
    r = client.patch(f"/api/v1/chat/sessions/{seeds['sid']}",
                     json={"title": "  renamed title  "}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "renamed title"  # trimmed

    sessions = client.get("/api/v1/chat/sessions", headers=headers).json()["sessions"]
    mine = [s for s in sessions if s["id"] == seeds["sid"]]
    assert mine and mine[0]["title"] == "renamed title"
