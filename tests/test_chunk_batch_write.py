"""Chunk writes go to the database in batches, not one connection per chunk.

The builder always holds a whole embedded batch when it stores, but it stored the
chunks one at a time: `save_chunk` and `store_l2_chunk` each open a connection and
commit, so a large document paid two connections and two fsyncs per chunk —
thousands of them, which is the ingestion bottleneck on long PDFs.

The assertions are about connections used and rows persisted, not about timing.
"""
import sqlite3

import pytest

from core.db import tenant_db as tenant_db_module
from core.db.tenant_db import TenantMetadataDB, TenantVectorDB


@pytest.fixture
def connection_counter(monkeypatch):
    """Count sqlite3.connect calls made while the test runs."""
    calls = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("database"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(tenant_db_module.sqlite3, "connect", counting_connect)
    return calls


def _chunk(idx: int, text: str) -> dict:
    return {
        "doc_id": "doc-1", "page_id": 1, "page_num": 1, "chunk_idx": idx,
        "section_path": "1.1", "section_title": "Specs", "chunk_text": text,
        "chunk_text_preview": text[:200],
    }


def _embedding_row(idx: int, text: str) -> dict:
    row = _chunk(idx, text)
    row["embedding"] = [0.1 * idx, 0.2, 0.3]
    return row


# ── vector store ─────────────────────────────────────────────────────────────

def test_chunk_embeddings_are_written_in_one_connection(tmp_path, connection_counter):
    vec = TenantVectorDB(tmp_path / "vec.db")
    connection_counter.clear()

    vec.store_l2_chunks([_embedding_row(i, f"text {i}") for i in range(4)])

    assert len(connection_counter) == 1, (
        f"batch write used {len(connection_counter)} connections"
    )
    with sqlite3.connect(vec.vec_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM l2_chunks").fetchone()[0] == 4


def test_the_single_chunk_writer_still_costs_one_connection_per_chunk(
        tmp_path, connection_counter):
    """Contrast: this is the cost the batch API exists to avoid."""
    vec = TenantVectorDB(tmp_path / "vec.db")
    connection_counter.clear()

    for i in range(4):
        vec.store_l2_chunk(page_id=1, chunk_idx=i, doc_id="doc-1",
                           embedding=[0.1 * i, 0.2], chunk_text=f"text {i}")

    assert len(connection_counter) == 4


# ── metadata store ───────────────────────────────────────────────────────────

def test_a_chunk_batch_reaches_the_table_and_the_fts_index(tmp_path, connection_counter):
    db = TenantMetadataDB(tmp_path / "metadata.db")
    connection_counter.clear()

    ids = db.save_chunks([
        _chunk(0, "alpha beta"),
        _chunk(1, "gamma delta"),
        _chunk(2, "epsilon zeta"),
    ])

    assert len(connection_counter) == 1, (
        f"batch write used {len(connection_counter)} connections"
    )
    assert len(ids) == 3 and len(set(ids)) == 3
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0] == 3
        hits = conn.execute(
            "SELECT COUNT(*) FROM doc_chunks_fts WHERE doc_chunks_fts MATCH 'gamma'"
        ).fetchone()[0]
    assert hits == 1, "the batch must be searchable through the FTS index"


def test_a_failing_batch_leaves_nothing_behind(tmp_path):
    """The caller falls back to per-chunk writes; a partial batch would duplicate."""
    db = TenantMetadataDB(tmp_path / "metadata.db")
    broken = _chunk(2, "epsilon zeta")
    del broken["chunk_idx"]

    with pytest.raises(KeyError):
        db.save_chunks([_chunk(0, "alpha beta"), _chunk(1, "gamma delta"), broken])

    with db.get_connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
    assert remaining == 0, "the failed batch must not leave rows for the retry to duplicate"
