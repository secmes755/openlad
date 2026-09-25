"""Diagnostic routes must close sqlite connections on every path.

#16: get_document_detail raised HTTPException(404) for a missing document
while its sqlite connection was still open, and the sibling endpoints
leaked theirs on any mid-query exception. The routes now wrap every
connection in contextlib.closing so the close is guaranteed.
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import core.api.routes.diagnostic as diag


class _ConnSpy:
    """Wraps a real sqlite3.Connection and records close() calls."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "closed", False)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)

    def close(self):
        object.__setattr__(self, "closed", True)
        return self._real.close()


@pytest.fixture
def tracked_conns(monkeypatch):
    conns = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        spy = _ConnSpy(real_connect(*args, **kwargs))
        conns.append(spy)
        return spy

    monkeypatch.setattr(diag.sqlite3, "connect", tracking_connect)
    return conns


@pytest.fixture
def admin_ctx(monkeypatch):
    monkeypatch.setattr(
        diag, "get_tenant_context",
        lambda: SimpleNamespace(user_role="admin", tenant_id="admin"),
    )


def _make_tenant_db(tenants_dir, tenant_id, with_doc):
    tdir = tenants_dir / tenant_id
    tdir.mkdir(parents=True)
    conn = sqlite3.connect(tdir / "metadata.db")
    conn.execute(
        "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, filename TEXT,"
        " doc_type TEXT, status TEXT, category_level1 TEXT, category_level2 TEXT,"
        " category_level3 TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE doc_pages (doc_id TEXT, page_type TEXT, section_path TEXT, section_title TEXT, page_num INTEGER)")
    conn.execute("CREATE TABLE doc_chunks (doc_id TEXT)")
    if with_doc:
        conn.execute(
            "INSERT INTO documents VALUES ('d1','T','f.pdf','datasheet','done',"
            " NULL,NULL,NULL,'{}','2026-01-01','2026-01-01')"
        )
    conn.commit()
    conn.close()


def test_404_path_closes_connection(tracked_conns, admin_ctx, monkeypatch, tmp_path):
    _make_tenant_db(tmp_path, "t1", with_doc=False)
    monkeypatch.setattr(diag.settings, "TENANTS_DIR", tmp_path)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(diag.get_document_detail("no-such-doc", "t1"))

    assert exc.value.status_code == 404
    assert tracked_conns, "endpoint never opened a connection"
    assert all(c.closed for c in tracked_conns), "404 path leaked a connection"


def test_success_path_closes_connection(tracked_conns, admin_ctx, monkeypatch, tmp_path):
    _make_tenant_db(tmp_path, "t1", with_doc=True)
    monkeypatch.setattr(diag.settings, "TENANTS_DIR", tmp_path)

    result = asyncio.run(diag.get_document_detail("d1", "t1"))

    assert result["status"] == "ok"
    assert all(c.closed for c in tracked_conns), "success path leaked a connection"


def test_error_path_closes_connection(tracked_conns, admin_ctx, monkeypatch, tmp_path):
    # Tenant db exists but has no tables -> sqlite error mid-query -> 500
    tdir = tmp_path / "t1"
    tdir.mkdir()
    sqlite3.connect(tdir / "metadata.db").close()
    monkeypatch.setattr(diag.settings, "TENANTS_DIR", tmp_path)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(diag.get_document_detail("d1", "t1"))

    assert exc.value.status_code == 500
    assert all(c.closed for c in tracked_conns), "error path leaked a connection"


def test_list_users_error_path_closes_connection(tracked_conns, admin_ctx, monkeypatch, tmp_path):
    # Point SYSTEM_DB_PATH at a non-database file -> query raises -> 500
    bogus = tmp_path / "not_a_db"
    bogus.write_text("this is not sqlite")
    monkeypatch.setattr(diag.settings, "SYSTEM_DB_PATH", bogus)
    # Isolate from the real system db singleton; the raw connection is what leaks
    monkeypatch.setattr(diag, "get_system_db", lambda: None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(diag.list_all_users())

    assert exc.value.status_code == 500
    assert tracked_conns and all(c.closed for c in tracked_conns), (
        "list_all_users error path leaked a connection"
    )
