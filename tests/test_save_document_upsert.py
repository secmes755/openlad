"""Regression test: save_document must UPSERT, not REPLACE.

INSERT OR REPLACE deletes the row and re-inserts it, which (1) resets
created_at to the current timestamp on every update and (2) resets columns
not passed in the update call (skill_id, topic_tags, default_permission,
is_mixed) to their defaults. The fix switches to ON CONFLICT DO UPDATE so
only the columns passed in are touched.
"""
from pathlib import Path

from core.db.tenant_db import TenantMetadataDB


def _make_db(tmp_path: Path) -> TenantMetadataDB:
    return TenantMetadataDB(tmp_path / "test_meta.db")


def test_save_document_preserves_created_at_and_omitted_columns(tmp_path):
    db = _make_db(tmp_path)
    doc_id = "doc123"
    db.save_document(doc_id, filename="a.pdf", title="Alpha",
                     status="pending", skill_id="semiconductor",
                     topic_tags=["chip", "uart"], default_permission="read",
                     is_mixed=False, created_at="2026-01-01 00:00:00")

    # Second call simulates the pending -> verified transition (builder.py
    # always passes filename + the new fields; created_at and the extra
    # columns like skill_id/topic_tags are NOT passed again).
    db.save_document(doc_id, filename="a.pdf", original_path="/x/a.pdf",
                     title="Alpha", status="verified",
                     file_hash="abc", text_source="direct_extract")

    row = db.get_document(doc_id)
    assert row is not None
    assert row["status"] == "verified"
    assert row["title"] == "Alpha"             # preserved
    assert row["skill_id"] == "semiconductor"  # preserved (would be reset by REPLACE)
    assert row["topic_tags"] == ["chip", "uart"]  # preserved
    assert row["default_permission"] == "read"    # preserved
    assert not row["is_mixed"]                     # preserved (SQLite BOOLEAN reads back as 0)
    assert row["created_at"] == "2026-01-01 00:00:00"  # NOT drifted to now


def test_save_document_insert_path_still_works(tmp_path):
    db = _make_db(tmp_path)
    db.save_document("fresh", filename="b.pdf", title="Beta", status="verified")
    row = db.get_document("fresh")
    assert row is not None
    assert row["title"] == "Beta"
    assert row["status"] == "verified"
