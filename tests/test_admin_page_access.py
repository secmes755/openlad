"""Admin page shell / API auth boundary tests — regression for 1c11a6b.

The admin page shell (/static/admin.html) must be reachable WITHOUT
Authorization: the frontend opens it via a plain anchor link, and browser
top-level navigation never sends Authorization headers. Every API call the
page makes sends its own Bearer header and stays behind auth, so a missing
or invalid key must yield 401.

The middleware's auth path touches the system DB on first request; the data
dir is isolated by tests/conftest.py so nothing leaks into the working tree.
"""
from fastapi.testclient import TestClient

from core.api.main import app

client = TestClient(app)


def test_admin_page_shell_public_without_auth():
    # Browser top-level navigation never sends Authorization — serving the
    # shell publicly is what lets a logged-in admin open the page at all.
    resp = client.get("/static/admin.html")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_api_missing_key_rejected():
    resp = client.get("/api/v1/documents")
    assert resp.status_code == 401


def test_api_invalid_key_rejected():
    resp = client.get("/api/v1/documents", headers={"Authorization": "Bearer not-a-real-key"})
    assert resp.status_code == 401
