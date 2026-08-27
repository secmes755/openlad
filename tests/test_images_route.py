"""HTTP-layer access rules for tenant-scoped image serving (GET /images/*).

The route must enforce, in order:
- authentication (401 without / with an invalid bearer key)
- filename whitelist (400 on path-traversal shapes and unknown extensions)
- tenant isolation (404 for a valid name that exists only in ANOTHER
  tenant's images directory — never leaks existence)
- happy path (200 with the stored bytes for the caller's own file)
"""
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api.main import app
from core.config import settings
from core.db.system_db import get_system_db
from core.tenant.auth import get_auth_manager
from core.tenant.tenant_manager import get_tenant_manager

client = TestClient(app)

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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
    _tenant_ready("mainimg")
    _tenant_ready("otherimg")
    key_main = _user_key("imgmain", "mainimg")
    key_other = _user_key("imgother", "otherimg")

    img_dir = Path(settings.get_tenant_images_dir("mainimg"))
    img_dir.mkdir(parents=True, exist_ok=True)
    target = img_dir / "docA_p1.png"
    target.write_bytes(_PNG_1PX)

    # A secret-looking file that lives ONLY in the other tenant's directory.
    other_dir = Path(settings.get_tenant_images_dir("otherimg"))
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "secret_p9.png").write_bytes(_PNG_1PX)

    return {"key_main": key_main, "key_other": key_other}


def test_missing_or_invalid_token_rejected(seeds):
    r1 = client.get("/images/docA_p1.png")
    assert r1.status_code == 401
    r2 = client.get("/images/docA_p1.png", headers={"Authorization": "Bearer not-a-key"})
    assert r2.status_code == 401


def test_own_tenant_image_served(seeds):
    r = client.get("/images/docA_p1.png",
                   headers={"Authorization": f"Bearer {seeds['key_main']}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == _PNG_1PX


def test_path_traversal_shapes_rejected(seeds):
    """Two independent defense layers, asserted separately:
    - encoded slashes break out of the single-segment route -> Starlette
      answers 404 before the handler ever runs;
    - slash-free shapes that decode into a handler-visible bad name
      (leading dots / backslash / fake extension) hit the filename
      whitelist -> 400.
    """
    headers = {"Authorization": f"Bearer {seeds['key_main']}"}
    # Slash-carrying shapes never reach the handler:
    r = client.get("/images/..%2F..%2Fsecret.png", headers=headers)
    assert r.status_code == 404
    r = client.get("/images/..%2Fsystem.db", headers=headers)
    assert r.status_code == 404
    # Slash-free garbage reaches the whitelist and gets a hard 400:
    for evil in ("%2e%2edata.db",          # '..data.db'  -> leading dots
                 "..%5Cevil.png",          # '..\evil.png' -> backslash
                 "docA_p1.png%2Ebak"):     # double extension
        r = client.get(f"/images/{evil}", headers=headers)
        assert r.status_code == 400, evil


def test_cross_tenant_file_is_404_not_200(seeds):
    # Same relative name exists in another tenant's images dir; the caller's
    # resolution root is their own directory, so it must read as not-found.
    r = client.get("/images/secret_p9.png",
                   headers={"Authorization": f"Bearer {seeds['key_main']}"})
    assert r.status_code == 404


def test_unknown_extension_rejected(seeds):
    r = client.get("/images/docA_p1.svg",
                   headers={"Authorization": f"Bearer {seeds['key_main']}"})
    assert r.status_code == 400
