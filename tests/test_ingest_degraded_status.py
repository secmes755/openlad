"""Ingest quality states: verified (zero anomalies) vs degraded (partial loss).

A document that lost chunks during ingestion is marked `degraded` with
human-readable `ingest_warnings` in its metadata; re-ingesting it cleanly
must clear the warnings and restore `verified`. Retrieval planning includes
degraded documents (informed, not excluded).
"""
from pathlib import Path

from core.db.tenant_db import TenantMetadataDB


def _make_db(tmp_path: Path) -> TenantMetadataDB:
    return TenantMetadataDB(tmp_path / "test_meta.db")


def test_degraded_document_persists_ingest_warnings(tmp_path):
    db = _make_db(tmp_path)
    db.save_document("d1", filename="a.pdf", title="A", status="degraded",
                     metadata={"ingest_warnings": ["272/304 chunks not embedded (rejected=272)"]})
    doc = db.get_document("d1")
    assert doc["status"] == "degraded"
    assert doc["metadata"]["ingest_warnings"] == ["272/304 chunks not embedded (rejected=272)"]


def test_successful_reingest_restores_verified_and_clears_warnings(tmp_path):
    """UPSERT replaces metadata_json wholesale: re-ingesting without warnings
    must clear stale ingest_warnings and flip status back to verified."""
    db = _make_db(tmp_path)
    db.save_document("d1", filename="a.pdf", title="A", status="degraded",
                     metadata={"ingest_warnings": ["272/304 chunks not embedded (rejected=272)"]})
    # Re-ingest, zero anomalies: builder always passes metadata (dict) + status
    db.save_document("d1", filename="a.pdf", title="A", status="verified",
                     metadata={})
    doc = db.get_document("d1")
    assert doc["status"] == "verified"
    assert "ingest_warnings" not in (doc.get("metadata") or {})


def test_get_all_documents_accepts_status_list(tmp_path):
    """Planner includes both verified and degraded docs (informed, not excluded)."""
    db = _make_db(tmp_path)
    db.save_document("v1", filename="v.pdf", status="verified")
    db.save_document("dg1", filename="d.pdf", status="degraded")
    db.save_document("p1", filename="p.pdf", status="processing")
    ids = {d["id"] for d in db.get_all_documents(status=["verified", "degraded"])}
    assert ids == {"v1", "dg1"}
    # single-value form still works (backward compatibility)
    ids_single = {d["id"] for d in db.get_all_documents(status="verified")}
    assert ids_single == {"v1"}
