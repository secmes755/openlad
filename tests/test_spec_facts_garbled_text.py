"""Facts must not be built from a PDF text layer that is font glyph codes.

The corpus contains annual reports whose embedded fonts carry no ToUnicode map.
pypdf renders those glyphs as ``(cid:NNN)`` — ordinary ASCII, no replacement
characters — so every check that looks at *character shapes* scores the page as
clean (``TextQualityChecker._detect_garbled`` counts replacement and control
characters, which these are not). The pipeline therefore built
authoritative-looking "spec facts" out of them: 258 of one tenant's 877 facts
(29%) came from three such documents, complete with ``attribute='cid'``.

Nothing in that text can be verified against the original, so it must not reach
the fact index, and the loss must be visible rather than silent — this is the
same contract as a page lost during analysis or a chunk that failed embedding.
"""
from core.config import settings
from core.ingestion.builder import DocumentIndexBuilder
from core.ingestion.text_quality import unmapped_glyph_ratio

PAGE_THRESHOLD = settings.TEXT_QUALITY_CONFIG["unmapped_glyph_page_threshold"]
FACT_THRESHOLD = settings.TEXT_QUALITY_CONFIG["unmapped_glyph_fact_threshold"]

GLYPHS = "(cid:4303)(cid:5988)(cid:6813)(cid:18172) (cid:6877)(cid:3531)(cid:4303)(cid:5988)(cid:6813)"
GARBLED_PAGE = GLYPHS * 4
PROSE = (
    "工作电压范围 3.0V 至 3.6V，典型值 3.3V，最大输出电流 800mA。"
    "The operating voltage is 3.3V nominal with a maximum output current of 800mA. "
) * 4


class _RecordingFacts:
    """Stands in for the metadata DB, recording inserted facts."""

    def __init__(self):
        self.facts = []

    def insert_spec_fact(self, **kwargs):
        self.facts.append(kwargs)


def _builder(monkeypatch, extractor):
    monkeypatch.setattr(
        "core.ingestion.spec_facts_extractor.extract_spec_facts_from_text", extractor)
    builder = object.__new__(DocumentIndexBuilder)      # bypass the heavy __init__
    db = _RecordingFacts()
    monkeypatch.setattr(builder, "_get_dbs", lambda tenant_id=None: (db, None))
    return builder, db


def _one_fact_per_page(text, page_num, entity, doc_id, extraction=None):
    return [{"doc_id": doc_id, "entity": entity, "attribute": "voltage", "value": "3.3",
             "page_num": page_num, "source_text": text, "verified": 1}]


def _run(builder, pages):
    return builder._extract_spec_facts("doc-1", pages, "admin", None, None)


# ── the ratio itself ─────────────────────────────────────────────────────────

def test_the_ratio_is_zero_without_glyphs():
    assert unmapped_glyph_ratio("") == 0.0
    assert unmapped_glyph_ratio("普通正文，没有字形码。") == 0.0


def test_the_ratio_is_near_one_for_a_page_of_glyphs():
    # 0.989 rather than 1.0: the separators between glyph tokens are not glyphs.
    assert unmapped_glyph_ratio(GARBLED_PAGE) > 0.95


# ── page gate ────────────────────────────────────────────────────────────────

def test_no_facts_are_taken_from_a_page_of_unmapped_glyphs(monkeypatch):
    builder, db = _builder(monkeypatch, _one_fact_per_page)

    warnings = _run(builder, [{"page_num": 1, "page_text": GARBLED_PAGE},
                              {"page_num": 2, "page_text": PROSE}])

    assert [fact["page_num"] for fact in db.facts] == [2], \
        "only the readable page may contribute facts"
    assert warnings, "a page that could not be read must not disappear silently"
    assert "1/2" in warnings[0] and "unmapped font glyphs" in warnings[0]


def test_a_page_that_is_mostly_prose_still_contributes_facts(monkeypatch):
    """The gate is a ratio, not a "contains glyphs" test: speckled prose is fine."""
    builder, db = _builder(monkeypatch, _one_fact_per_page)
    speckled = PROSE + "(cid:1)(cid:2)(cid:3)"
    assert unmapped_glyph_ratio(speckled) < PAGE_THRESHOLD

    warnings = _run(builder, [{"page_num": 1, "page_text": speckled}])

    assert len(db.facts) == 1
    assert warnings == [], "nothing was skipped, so nothing should be reported"


# ── fact gate ────────────────────────────────────────────────────────────────

def test_a_fact_whose_own_source_line_is_glyphs_is_dropped(monkeypatch):
    """A page can be mostly readable and still contain an unreadable fact."""

    def two_facts(text, page_num, entity, doc_id, extraction=None):
        return [
            {"doc_id": doc_id, "entity": entity, "attribute": "voltage", "value": "3.3",
             "page_num": page_num, "source_text": PROSE[:60], "verified": 1},
            {"doc_id": doc_id, "entity": entity, "attribute": "cid", "value": "6813)",
             "page_num": page_num, "source_text": GARBLED_PAGE, "verified": 1},
        ]

    builder, db = _builder(monkeypatch, two_facts)
    page_text = PROSE + "(cid:1)(cid:2)"

    warnings = _run(builder, [{"page_num": 1, "page_text": page_text}])

    assert [fact["attribute"] for fact in db.facts] == ["voltage"]
    assert warnings and "facts dropped" in warnings[0]
    assert FACT_THRESHOLD <= unmapped_glyph_ratio(GARBLED_PAGE)


# ── the happy path must stay quiet ───────────────────────────────────────────

def test_a_readable_document_reports_nothing(monkeypatch):
    builder, db = _builder(monkeypatch, _one_fact_per_page)

    warnings = _run(builder, [{"page_num": 1, "page_text": PROSE},
                              {"page_num": 2, "page_text": PROSE}])

    assert len(db.facts) == 2
    assert warnings == []


def test_a_disabled_fact_index_stays_silent(monkeypatch):
    monkeypatch.setitem(settings.CONTEXT_CONFIG, "spec_facts_enabled", False)
    builder, db = _builder(monkeypatch, _one_fact_per_page)

    warnings = _run(builder, [{"page_num": 1, "page_text": GARBLED_PAGE}])

    assert warnings == []
    assert db.facts == []


def test_the_warning_marks_the_document_degraded(monkeypatch):
    """Warnings are useless unless they reach the status the retrieval side reads."""
    builder, _ = _builder(monkeypatch, _one_fact_per_page)

    warnings = _run(builder, [{"page_num": 1, "page_text": GARBLED_PAGE}])
    all_warnings = DocumentIndexBuilder._collect_ingest_warnings(
        [], {}, [], warnings)

    assert all_warnings == warnings
    assert ("degraded" if all_warnings else "verified") == "degraded"
