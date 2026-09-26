"""Tenant ingestion lock: concurrent ingests for one tenant serialize,
different tenants still run in parallel.

The builder is an app-state singleton. Ingestion mutates shared,
per-ingest state (chart analyzer wiring, OCR temp files, metadata
writes) and had NO serialization: two uploads for the same tenant raced
each other's OCR temp files and metadata writes. The fix serializes
ingest_document per tenant with a blocking lock (uploads run in
background tasks, so queuing does not degrade the request path) while
keeping cross-tenant parallelism.
"""
import threading
import time

from core.ingestion.builder import DocumentIndexBuilder


class _FakeParsedPage:
    raw_text = "page text"


class _FakeParsedDoc:
    def __init__(self, name):
        self.filename = name
        self.original_path = f"/tmp/{name}"
        self.file_size = 100
        self.total_pages = 1
        self.pages = [_FakeParsedPage()]
        self.metadata = {}


class _FakeMetadataDB:
    db_path = "fake"
    def get_document_by_hash(self, h):
        return None
    def save_document(self, **kw):
        pass
    def get_document(self, doc_id):
        return {"id": doc_id, "file_hash": "x"}


def _make_builder(monkeypatch, intervals, gate):
    b = DocumentIndexBuilder.__new__(DocumentIndexBuilder)
    b.tenant_id = None
    b._ingest_locks = {}
    b._ingest_locks_guard = threading.Lock()
    monkeypatch.setattr(b, "_get_dbs", lambda tid=None: (_FakeMetadataDB(), None))
    monkeypatch.setattr(b, "_preprocess_document",
                        lambda *a, **k: gate())
    monkeypatch.setattr(b, "_determine_text_source", lambda pages: "text")
    class _Parser:
        def parse(self, path):
            return _FakeParsedDoc(path)
    b.parser = _Parser()
    return b


def _gate(intervals, hold=0.4):
    """Record an occupancy interval; long enough that unsynchronized
    entrants reliably overlap."""
    start = time.monotonic()
    time.sleep(hold)
    intervals.append((start, time.monotonic()))
    return []


def _run_ingest(builder, tmp_path, name, tenant, errors):
    f = tmp_path / name
    f.write_bytes(b"data")
    try:
        builder.ingest_document(str(f), tenant_id=tenant)
    except Exception as e:
        errors.append(e)


def _overlaps(iv):
    (s1, e1), (s2, e2) = iv
    return s1 < e2 and s2 < e1


def test_same_tenant_ingests_serialize(tmp_path, monkeypatch):
    intervals = []
    builder = _make_builder(monkeypatch, intervals,
                            lambda: _gate(intervals))
    errors = []
    t1 = threading.Thread(target=_run_ingest,
                          args=(builder, tmp_path, "a.pdf", "tenantA", errors))
    t2 = threading.Thread(target=_run_ingest,
                          args=(builder, tmp_path, "b.pdf", "tenantA", errors))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors
    assert len(intervals) == 2
    assert not _overlaps(intervals), (
        f"same-tenant ingests overlapped: {intervals}")


def test_different_tenants_still_parallel(tmp_path, monkeypatch):
    intervals = []
    builder = _make_builder(monkeypatch, intervals,
                            lambda: _gate(intervals))
    errors = []
    t1 = threading.Thread(target=_run_ingest,
                          args=(builder, tmp_path, "a.pdf", "tenantA", errors))
    t2 = threading.Thread(target=_run_ingest,
                          args=(builder, tmp_path, "b.pdf", "tenantB", errors))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors
    assert len(intervals) == 2
    assert _overlaps(intervals), (
        f"cross-tenant ingests must stay parallel: {intervals}")
