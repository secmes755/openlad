"""TenantVectorDB must close its raw sqlite connections on error paths.

#18: search_l2_chunks returned [] from its outer except without closing
the connection whenever struct packing or the query itself raised; the
same unprotected pattern existed in store_l2_chunk, delete_doc_vectors,
and _init_vec_db. store_l2_chunks already demonstrated the correct
try/finally pattern — it is now applied to the whole class.
"""

import sqlite3

import pytest

import core.db.tenant_db as tenant_db
from core.db.tenant_db import TenantVectorDB


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

    monkeypatch.setattr(tenant_db.sqlite3, "connect", tracking_connect)
    return conns


def _bare_db(path):
    """TenantVectorDB instance without __init__ (skip schema creation)."""
    db = TenantVectorDB.__new__(TenantVectorDB)
    db.vec_db_path = path
    return db


def test_search_l2_chunks_error_path_closes_connection(tracked_conns, tmp_path):
    # Real but empty sqlite file -> 'no such table: l2_chunks' at query time
    empty = tmp_path / "vec.db"
    sqlite3.connect(empty).close()

    result = _bare_db(empty).search_l2_chunks([0.1] * 8)

    assert result == []
    assert tracked_conns and all(c.closed for c in tracked_conns), (
        "search_l2_chunks leaked its connection on the query-error path"
    )


def test_search_l2_chunks_success_path_closes_connection(tracked_conns, tmp_path):
    vec = tmp_path / "vec.db"
    conn = sqlite3.connect(vec)
    conn.execute(
        "CREATE TABLE l2_chunks (page_id INTEGER, chunk_idx INTEGER, doc_id TEXT,"
        " embedding BLOB, chunk_text_preview TEXT, chunk_text TEXT,"
        " PRIMARY KEY (page_id, chunk_idx))"
    )
    conn.commit()
    conn.close()

    result = _bare_db(vec).search_l2_chunks([0.1] * 8)

    assert result == []
    assert tracked_conns and all(c.closed for c in tracked_conns), (
        "search_l2_chunks leaked its connection on the success path"
    )


def test_store_l2_chunk_error_path_closes_connection(tracked_conns, tmp_path):
    empty = tmp_path / "vec.db"
    sqlite3.connect(empty).close()

    _bare_db(empty).store_l2_chunk(1, 0, "doc1", [0.1] * 8, "preview", "text")

    assert tracked_conns and all(c.closed for c in tracked_conns), (
        "store_l2_chunk leaked its connection on the insert-error path"
    )


def test_delete_doc_vectors_error_path_closes_connection(tracked_conns, tmp_path):
    empty = tmp_path / "vec.db"
    sqlite3.connect(empty).close()

    result = _bare_db(empty).delete_doc_vectors("doc1")

    assert result is False
    assert tracked_conns and all(c.closed for c in tracked_conns), (
        "delete_doc_vectors leaked its connection on the delete-error path"
    )


def test_init_vec_db_error_path_closes_connection(tracked_conns, tmp_path):
    # Not a database file -> CREATE TABLE raises mid-init
    bogus = tmp_path / "vec.db"
    bogus.write_text("this is not sqlite")

    TenantVectorDB(bogus)  # logs the init failure, must not raise

    assert tracked_conns and all(c.closed for c in tracked_conns), (
        "_init_vec_db leaked its connection on the schema-error path"
    )
