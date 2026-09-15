"""A page whose text layer is font glyph codes must not reach the index.

``(cid:NNN)`` is what a reader shows for a glyph with no ToUnicode mapping. It is
ordinary ASCII, so the garbled-character checks see nothing wrong with it, and the
page used to be stored as-is — its chunks then went into the FTS *and* the vector
index. The fact extractor already skipped such pages, so facts stayed clean while
retrieval was polluted. They are now dropped at the door and named in the ingest
warnings, which is what keeps the loss visible instead of silent.
"""
from types import SimpleNamespace

from core.ingestion.builder import DocumentIndexBuilder

# A whole page rendered from a font with no ToUnicode map.
GLYPH_PAGE = "(cid:6813)(cid:6814)(cid:6815)(cid:6816)" * 20
GOOD_PAGE = "The T536 supports DDR3 and LPDDR4 memory. " * 5

PAGES = [
    SimpleNamespace(page_num=1, section_title="S1", raw_text=GOOD_PAGE, content_dict={}),
    SimpleNamespace(page_num=2, section_title="S2", raw_text=GLYPH_PAGE, content_dict={}),
]
PREPROCESSED = [
    SimpleNamespace(raw_text=p.raw_text, text_source="direct_extract", ocr_results=[],
                    ocr_confidence=None, page_image_path=None)
    for p in PAGES
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


def _builder(monkeypatch):
    builder = DocumentIndexBuilder(tenant_id="admin")
    meta = _DummyMetaDB()
    monkeypatch.setattr(builder, "_get_dbs", lambda tid=None: (meta, None))
    monkeypatch.setattr(builder, "_build_structure_index",
                        lambda doc_id, page_results, parsed_doc: ({}, []))
    monkeypatch.setattr(builder, "_save_structure_index_to_db", lambda *a, **k: None)
    summary_calls = []

    def fake_summary(text, page_num):
        summary_calls.append((page_num, text))
        return "summary"

    monkeypatch.setattr(builder, "_generate_page_summary", fake_summary)
    return builder, meta, summary_calls


def test_unreadable_page_is_not_indexed_and_is_named(monkeypatch):
    builder, meta, summary_calls = _builder(monkeypatch)
    parsed_doc = SimpleNamespace(filename="d.pdf", pages=PAGES)

    l2_results, warnings = builder._build_l2("doc-1", parsed_doc, PREPROCESSED, "admin")

    stored = {row["page_num"]: row for row in meta.saved}
    assert stored[1]["raw_text"] == GOOD_PAGE, "a readable page keeps its text"
    assert stored[2]["raw_text"] == "", "glyph codes must not reach the page index"
    assert stored[2]["page_summary"] == "", "nothing may be derived from glyph codes"
    assert not stored[2]["entities"]

    # The page summary is an LLM call: it must not be spent on unreadable text.
    assert [page_num for page_num, _ in summary_calls] == [1]

    # Chunks are built from the stored page text, so an unreadable page yields
    # none — that is what keeps the FTS and vector channels clean.
    assert {row["page_num"]: row["page_text"] for row in l2_results}[2] == ""

    assert any("unreadable text layer" in w and "[2]" in w for w in warnings), warnings
    assert ("degraded" if builder._collect_ingest_warnings([], {}, warnings) else "verified") == "degraded"


def test_prose_that_merely_mentions_a_glyph_code_keeps_its_text(monkeypatch):
    """The gate catches pages that *are* glyph codes, not prose containing one."""
    builder, meta, _ = _builder(monkeypatch)
    text = GOOD_PAGE + "(cid:12)"
    pages = [SimpleNamespace(page_num=1, section_title="S", raw_text=text, content_dict={})]
    preprocessed = [SimpleNamespace(raw_text=text, text_source="direct_extract", ocr_results=[],
                                    ocr_confidence=None, page_image_path=None)]
    parsed_doc = SimpleNamespace(filename="d.pdf", pages=pages)

    _, warnings = builder._build_l2("doc-1", parsed_doc, preprocessed, "admin")

    assert meta.saved[0]["raw_text"] == text
    assert warnings == []


def test_ocr_text_is_never_treated_as_an_unreadable_layer(monkeypatch):
    """OCR output is recovered content: it cannot contain glyph codes, and a page
    recovered that way must stay indexed."""
    builder, meta, _ = _builder(monkeypatch)
    pages = [SimpleNamespace(page_num=1, section_title="S", raw_text="", content_dict={})]
    preprocessed = [SimpleNamespace(raw_text="OCR recovered this page", text_source="ocr",
                                    ocr_results=[], ocr_confidence=0.9, page_image_path=None)]
    parsed_doc = SimpleNamespace(filename="d.pdf", pages=pages)

    _, warnings = builder._build_l2("doc-1", parsed_doc, preprocessed, "admin")

    assert meta.saved[0]["raw_text"] == "OCR recovered this page"
    assert meta.saved[0]["text_source"] == "ocr"
    assert warnings == []
