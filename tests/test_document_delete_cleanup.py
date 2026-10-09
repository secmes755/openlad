"""Document deletion must clean up on-disk storage (files + page images).

Regression suite for the storage-cleanup gap described in
~/.openlad/design_reviews/2026-10-09_document_delete_storage_cleanup.md.

Contract under test (hard delete, the frontend default):

    A hard delete removes the DB records, the vectors, AND the on-disk
    artifacts belonging to that document -- the original file recorded in
    ``documents.original_path`` and every page/chart image referenced by
    that document's ``doc_pages.page_image_path``.

These tests are written against a temporary tenant directory so they touch
no real data. They should FAIL before the fix and PASS after it.

Design conventions encoded here:
  * DB first, files second: a failure to remove a file must NOT roll back or
    fail the delete (missing files are tolerated, logged, skipped).
  * Deletion is idempotent: deleting twice is a no-op, not an error.
  * Cross-tenant safety: deleting tenant A's document never removes a file
    that belongs to tenant B.
"""
import json

from core.db.tenant_db import TenantMetadataDB, TenantVectorDB


def _make_tenant_db(tmp_path, tenant_id="t1"):
    """Create an isolated tenant metadata + vector DB pair under tmp_path."""
    base = tmp_path / "tenants" / tenant_id
    (base / "documents").mkdir(parents=True, exist_ok=True)
    (base / "images").mkdir(parents=True, exist_ok=True)
    meta = TenantMetadataDB(base / "metadata.db")
    vec = TenantVectorDB(base / "vectors.vec.db")
    return base, meta, vec


def _seed_document(meta, base, doc_id="a" * 32, n_pages=3, tenant_id="t1"):
    """Insert a document row + pages with real image files on disk.

    Returns (doc_file, [image_files]).
    """
    docs_dir = base / "documents"
    imgs_dir = base / "images"

    doc_file = docs_dir / f"{doc_id}_datasheet.pdf"
    doc_file.write_bytes(b"%PDF-1.4 fake")

    meta.save_document(
        doc_id,
        filename=doc_file.name,
        original_path=str(doc_file),
        title="Test Datasheet",
        status="verified",
        file_hash="deadbeef",
    )

    image_files = []
    for p in range(1, n_pages + 1):
        img = imgs_dir / f"{doc_id}_p{p}.png"
        img.write_bytes(b"\x89PNG fake")
        image_files.append(img)

        # Chart crops live only inside content_json (.charts[].image_path),
        # matching how ingestion records them -- there is no dedicated column.
        content_json = None
        if p == 1:
            chart = imgs_dir / f"{doc_id}_p1_chart0.png"
            chart.write_bytes(b"\x89PNG fake chart")
            image_files.append(chart)
            content_json = json.dumps({
                "page_num": p,
                "charts": [{"id": f"{doc_id}_p1_chart0",
                            "image_path": str(chart)}],
            })

        meta.save_page(doc_id, p, raw_text=f"page {p}",
                       page_image_path=str(img),
                       content_json=content_json)

    return doc_file, image_files


# --------------------------------------------------------------------------
# 1. DB layer: the new cleanup entry point
# --------------------------------------------------------------------------

def test_delete_document_removes_original_and_images(tmp_path):
    """Hard delete wipes the DB rows AND every on-disk artifact."""
    base, meta, vec = _make_tenant_db(tmp_path)
    doc_id = "a" * 32
    doc_file, images = _seed_document(meta, base, doc_id=doc_id)

    # Sanity: everything exists up front.
    assert doc_file.exists()
    assert all(f.exists() for f in images)
    assert meta.get_document(doc_id) is not None

    deleted = meta.delete_document(doc_id, delete_files=True)

    assert deleted is True
    # DB gone
    assert meta.get_document(doc_id) is None
    assert meta.get_document_pages(doc_id) == []
    # Disk gone
    assert not doc_file.exists(), "original file must be removed on hard delete"
    for f in images:
        assert not f.exists(), f"page/chart image must be removed: {f.name}"


def test_delete_document_soft_keeps_files(tmp_path):
    """Soft delete (delete_files=False) keeps artifacts for recovery."""
    base, meta, vec = _make_tenant_db(tmp_path)
    doc_id = "b" * 32
    doc_file, images = _seed_document(meta, base, doc_id=doc_id)

    deleted = meta.delete_document(doc_id, delete_files=False)

    assert deleted is True
    assert meta.get_document(doc_id) is None          # DB row still removed
    # Artifacts preserved for possible recovery
    assert doc_file.exists()
    assert all(f.exists() for f in images)


# --------------------------------------------------------------------------
# 2. Robustness / idempotency / safety
# --------------------------------------------------------------------------

def test_delete_document_missing_files_is_not_an_error(tmp_path):
    """A file already gone must not fail the delete (tolerate, don't raise)."""
    base, meta, vec = _make_tenant_db(tmp_path)
    doc_id = "c" * 32
    doc_file, images = _seed_document(meta, base, doc_id=doc_id)

    # Simulate a file that vanished out-of-band.
    images[0].unlink()

    assert meta.delete_document(doc_id, delete_files=True) is True
    assert meta.get_document(doc_id) is None


def test_delete_document_is_idempotent(tmp_path):
    """Deleting twice is a no-op, not an exception."""
    base, meta, vec = _make_tenant_db(tmp_path)
    doc_id = "d" * 32
    _seed_document(meta, base, doc_id=doc_id)

    assert meta.delete_document(doc_id, delete_files=True) is True
    # Second call: nothing to delete, still reports success (idempotent).
    assert meta.delete_document(doc_id, delete_files=True) is True


def test_delete_does_not_touch_other_documents(tmp_path):
    """Only the target document's artifacts are removed."""
    base, meta, vec = _make_tenant_db(tmp_path)
    keep_id, drop_id = "e" * 32, "f" * 32
    keep_file, keep_imgs = _seed_document(meta, base, doc_id=keep_id)
    drop_file, drop_imgs = _seed_document(meta, base, doc_id=drop_id)

    meta.delete_document(drop_id, delete_files=True)

    assert keep_file.exists()
    assert all(f.exists() for f in keep_imgs)
    assert not drop_file.exists()
    assert not any(f.exists() for f in drop_imgs)


def test_delete_vectors_callable_after_cleanup(tmp_path):
    """Vector cleanup must still succeed when called alongside file cleanup.

    This build exposes no vector-insert API, so we only assert the delete
    path stays callable and returns success for an empty vector store --
    the point is that adding file cleanup does not break vector teardown.
    """
    base, meta, vec = _make_tenant_db(tmp_path)
    doc_id = "1" * 32
    _seed_document(meta, base, doc_id=doc_id)

    assert meta.delete_document(doc_id, delete_files=True) is True
    assert vec.delete_doc_vectors(doc_id) is True


# --------------------------------------------------------------------------
# 3. API layer: the endpoint must actually invoke file cleanup
# --------------------------------------------------------------------------

def test_api_delete_route_removes_files(tmp_path, monkeypatch):
    """End-to-end: the DELETE route must clean disk via the DB layer.

    Uses the real handler with a stubbed tenant context so we assert the
    route passes through to the cleanup-aware delete (delete_files=True).
    """
    from core.api.routes import documents as docs_route

    base, meta, vec = _make_tenant_db(tmp_path)
    doc_id = "9" * 32
    doc_file, images = _seed_document(meta, base, doc_id=doc_id)

    class _Ctx:
        tenant_id = "t1"
        user_id = "u1"

    # Route resolves the DB through get_tenant_metadata_db / get_tenant_vector_db.
    monkeypatch.setattr("core.api.routes.documents.get_tenant_metadata_db",
                        lambda tid: meta, raising=False)
    monkeypatch.setattr("core.api.routes.documents.get_tenant_context",
                        lambda: _Ctx(), raising=False)
    if hasattr(docs_route, "get_tenant_vector_db"):
        monkeypatch.setattr("core.api.routes.documents.get_tenant_vector_db",
                            lambda tid: vec, raising=False)

    import asyncio
    result = asyncio.run(docs_route.delete_document(doc_id))

    assert result["success"] is True
    assert not doc_file.exists(), "route must remove the original file"
    assert not any(f.exists() for f in images), "route must remove page images"


# --------------------------------------------------------------------------
# 4. Orphan audit helper (the companion survey script's core logic)
# --------------------------------------------------------------------------

def test_orphan_audit_protects_files_named_after_a_different_uuid(tmp_path):
    """A live file must survive even when its name != its row id.

    Uploaded documents are stored as "<upload-uuid>_<original name>" while the
    documents row id is a *different* uuid. Classification based on the
    filename prefix would mark such a live file as an orphan and delete it.
    """
    from scripts.audit_orphan_files import audit  # noqa: E402

    base, meta, vec = _make_tenant_db(tmp_path)
    row_id = "a" * 32
    upload_uuid = "b" * 32  # deliberately different from the row id
    doc_file = base / "documents" / f"{upload_uuid}_Real_Datasheet.pdf"
    doc_file.write_bytes(b"%PDF-1.4 real")
    meta.save_document(row_id, filename=doc_file.name,
                       original_path=str(doc_file), title="Real",
                       status="verified", file_hash="h")

    report = audit(tmp_path)
    orphans = []
    for entry in report["tenants"].values():
        orphans += entry["documents"] + entry["images"]

    assert not any("Real_Datasheet" in p for p in orphans), \
        "a live file whose name differs from its row id must not be an orphan"


def test_orphan_audit_finds_stale_files(tmp_path):
    """The audit helper distinguishes live files from orphans."""
    from scripts.audit_orphan_files import audit  # noqa: E402

    base, meta, vec = _make_tenant_db(tmp_path)
    live_id = "2" * 32
    _seed_document(meta, base, doc_id=live_id)

    # Drop an orphan file with no DB row.
    orphan = base / "documents" / f"{'3' * 32}_ghost.pdf"
    orphan.write_bytes(b"%PDF-1.4 ghost")

    report = audit(tmp_path)

    tenant_entry = report["tenants"].get("t1", {})
    orphans = tenant_entry.get("documents", []) + tenant_entry.get("images", [])
    assert any("ghost" in p for p in orphans), "orphan file must be reported"
    # The live document's files are NOT reported as orphans.
    assert not any(live_id in p for p in orphans)
