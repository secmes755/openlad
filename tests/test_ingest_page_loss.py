"""A page that fails analysis must not vanish without a trace.

``_build_l2`` left a failed page out of the index with only a log line, after
which the document was saved as *verified* while silently missing content —
retrieval had no way to flag the gap. Page losses now flow through
``_collect_ingest_warnings``, which is what decides verified vs degraded.
"""
from types import SimpleNamespace

from core.ingestion.builder import DocumentIndexBuilder

PAGES = [
    SimpleNamespace(page_num=n, section_title=f"S{n}", raw_text=f"text {n}", content_dict={})
    for n in (1, 2, 3)
]
PREPROCESSED = [
    SimpleNamespace(raw_text=f"text {n}", text_source="direct_extract",
                    ocr_results=[], ocr_confidence=None, page_image_path=None)
    for n in (1, 2, 3)
]


class _DummyMetaDB:
    def __init__(self):
        self.saved = []

    def save_page(self, **kwargs):
        self.saved.append(kwargs)
        return len(self.saved)

    def __getattr__(self, name):
        """Any other metadata call the index build makes is a no-op here."""
        def _noop(*args, **kwargs):
            return None
        return _noop


def _builder(monkeypatch, failing_page):
    builder = DocumentIndexBuilder(tenant_id="admin")
    meta = _DummyMetaDB()
    monkeypatch.setattr(builder, "_get_dbs", lambda tid=None: (meta, None))
    monkeypatch.setattr(builder, "_build_structure_index",
                        lambda doc_id, page_results, parsed_doc: ({}, []))
    monkeypatch.setattr(builder, "_save_structure_index_to_db", lambda *a, **k: None)

    def fake_summary(text, page_num):
        if page_num == failing_page:
            raise RuntimeError("page analysis blew up")
        return "summary"

    monkeypatch.setattr(builder, "_generate_page_summary", fake_summary)
    return builder, meta


def test_failed_page_is_named_and_the_document_becomes_degraded(monkeypatch):
    builder, meta = _builder(monkeypatch, failing_page=2)
    parsed_doc = SimpleNamespace(filename="d.pdf", pages=PAGES)

    l2_results, warnings = builder._build_l2("doc-1", parsed_doc, PREPROCESSED, "admin")

    assert len(l2_results) == 2, "the failing page must not be indexed"
    assert len(meta.saved) == 2
    assert warnings, "a dropped page must produce an ingest warning"
    assert "[2]" in warnings[0], f"the failing page must be named: {warnings}"

    # The warning list is what flips the document's status, and retrieval reads
    # the same list back out of the document metadata.
    all_warnings = builder._collect_ingest_warnings([], {}, warnings)
    assert all_warnings == warnings
    assert ("degraded" if all_warnings else "verified") == "degraded"


def test_a_complete_run_produces_no_warnings(monkeypatch):
    builder, meta = _builder(monkeypatch, failing_page=None)
    parsed_doc = SimpleNamespace(filename="d.pdf", pages=PAGES)

    l2_results, warnings = builder._build_l2("doc-1", parsed_doc, PREPROCESSED, "admin")

    assert len(l2_results) == 3
    assert len(meta.saved) == 3
    assert warnings == []
    assert ("degraded" if builder._collect_ingest_warnings([], {}, warnings) else "verified") == "verified"
