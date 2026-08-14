"""Ingestion pipeline pure-logic checks (spec-facts rule extractor, synthetic text)."""
from core.ingestion.spec_facts_extractor import (
    _clean,
    _num_word_to_int,
    extract_spec_facts_from_text,
    infer_doc_entity,
    strip_vlm_blocks,
)


# ---- VLM block stripping ----
def test_strip_vlm_blocks_removes_ai_block():
    text = ("Real content first line.\n"
            "--- Page Visual Analysis (VLM)\n"
            "AI hallucination: 560 solder balls\n")
    out = strip_vlm_blocks(text)
    assert "Real content" in out
    assert "560" not in out


def test_strip_vlm_blocks_plain_text_unchanged():
    text = "Just normal content."
    assert strip_vlm_blocks(text) == text


# ---- number words ----
def test_num_word_to_int():
    assert _num_word_to_int("ten") == 10
    assert _num_word_to_int("Dual") == 2
    assert _num_word_to_int("8") == 8
    assert _num_word_to_int("abc") is None


# ---- clean ----
def test_clean_normalizes_whitespace_and_strips_punctuation():
    assert _clean("  ball size , ") == "ball size"
    assert _clean("GPU: Mali ") == "GPU: Mali"


# ---- entity inference ----
def test_infer_doc_entity_from_title_and_filename():
    # Core is industry-agnostic: model patterns arrive via the industry pack's
    # entity_patterns hook (RetrievalPlugin.get_entity_patterns); the test
    # supplies them explicitly, as the ingestion builder does at runtime.
    patterns = [r"(RK\d{4}[A-Z]?)"]
    assert infer_doc_entity("Rockchip RK3588 Datasheet V1.9", entity_patterns=patterns) == "RK3588"
    assert infer_doc_entity("", "RK3588 Datasheet.pdf", entity_patterns=patterns) == "RK3588"


def test_infer_doc_entity_falls_back_to_cleaned_title():
    # No model pattern found -> fall back to the cleaned source text (by design)
    assert infer_doc_entity("", "T536_Datasheet_V0.91.pdf") == "T536_Datasheet_V0.91.pdf"
    assert infer_doc_entity("No model here") == "No model here"


# ---- rule extraction ----
# Industry-pack vocabulary for the extractor (core keeps only mechanisms).
SEMICON_EXTRACTION = {
    "spec_headers": ["gpu", "cpu", "npu", "package", "process", "memory"],
    "compute_units": ["TOPS"],
    "compute_attribute": "compute power",
    "frequency_terms": ["frequency", "clock"],
}


def test_extract_spec_facts_support_sentence():
    facts = extract_spec_facts_from_text(
        "The chip supports ten UART interfaces.", 1, "RK3588", "d1",
        extraction=SEMICON_EXTRACTION)
    # attribute keeps the surface case of the feature word (not lowercased)
    assert any(f["attribute"] == "UART interfaces count" and f["value"] == "10"
               for f in facts)


def test_extract_spec_facts_key_value():
    facts = extract_spec_facts_from_text(
        "ball size: 0.35mm\nball pitch: 0.65mm", 1, "RK3588", "d1",
        extraction=SEMICON_EXTRACTION)
    assert any(f["attribute"] == "ball size" and f["value"] == "0.35mm"
               for f in facts)


def test_extract_spec_facts_skips_vlm_hallucinations():
    raw = ("Real spec line: ball size: 0.35mm\n"
           "--- Page Visual Analysis (VLM)\n"
           "The chip has 560 solder balls.")
    facts = extract_spec_facts_from_text(raw, 1, "RK3588", "d1",
                                         extraction=SEMICON_EXTRACTION)
    assert any(f["value"] == "0.35mm" for f in facts)
    assert all(f["value"] != "560" for f in facts)


# ---- TOPS strictness (regression: "2.2 Top Marking" / "1 Top frame mode") ----
def test_extract_spec_facts_tops_plural_only():
    # Real compute declarations carry the plural unit.
    facts = extract_spec_facts_from_text(
        "Neural network acceleration engine with processing performance up to 1 TOPS.",
        6, "RK3568", "d1", extraction=SEMICON_EXTRACTION)
    assert any(f["attribute"] == "compute power" and f["value"] == "1 TOPS"
               for f in facts)


def test_extract_spec_facts_tops_rejects_top_marking_heading():
    # Datasheet section heading "2.2 Top Marking" (silkscreen) must NOT be
    # extracted as compute power — "Top" is not the TOPS unit.
    facts = extract_spec_facts_from_text(
        "2.2 Top Marking\nBrand: Rockchip\nPart Number: RK3568",
        19, "RK3568", "d1", extraction=SEMICON_EXTRACTION)
    assert all(f["attribute"] != "compute power" for f in facts)


def test_extract_spec_facts_tops_rejects_top_frame_mode():
    # Video scan mode "Output 1 Top frame mode" must not be read as 1 TOP.
    facts = extract_spec_facts_from_text(
        "I5O1T: Input 5 Fields Output 1 Top frame mode", 10, "RK3568", "d1",
        extraction=SEMICON_EXTRACTION)
    assert all(f["attribute"] != "compute power" for f in facts)


# ---- frequency/clock extraction (industry-agnostic attribute capture) ----
def test_extract_spec_facts_frequency_free_text():
    facts = extract_spec_facts_from_text(
        "Max frequency for CPU Frequency NA NA 2 GHz", 24, "RK3562", "d1",
        extraction=SEMICON_EXTRACTION)
    assert any(f["value"] == "2 GHz" for f in facts)


def test_extract_spec_facts_frequency_na_tbd_never_matches():
    # TBD/NA cells carry no digits before the unit -> no fact.
    facts = extract_spec_facts_from_text(
        "Max CPU frequency NA NA TBD GHz", 55, "RK3568", "d1",
        extraction=SEMICON_EXTRACTION)
    assert all("GHz" not in (f.get("value") or "") for f in facts)


def test_extract_spec_facts_frequency_colon_form():
    facts = extract_spec_facts_from_text(
        "Max NPU frequency: 1.0 GHz", 6, "RK3572", "d1",
        extraction=SEMICON_EXTRACTION)
    assert any(f["value"] == "1.0 GHz" for f in facts)


# ---- vocabulary-boundary degradation: no pack = structural patterns only ----
def test_extract_spec_facts_no_vocab_disables_compute_and_freq():
    # Without an industry pack vocabulary, compute-power and frequency
    # patterns are disabled (core keeps mechanisms, not word lists).
    text = ("Neural network engine up to 1 TOPS\n"
            "Max CPU frequency NA NA 2 GHz")
    facts = extract_spec_facts_from_text(text, 1, "RK3568", "d1")
    assert all(f["attribute"] != "compute power" for f in facts)
    assert all("GHz" not in (f.get("value") or "") for f in facts)


def test_extract_spec_facts_two_line_header_from_pack():
    # Two-line "GPU\n Mali-G52 1-Core-2EE" layout only extracts when the
    # header word is provided by the pack.
    text = "GPU\n Mali-G52 1-Core-2EE"
    facts = extract_spec_facts_from_text(text, 1, "RK3568", "d1",
                                         extraction=SEMICON_EXTRACTION)
    assert any(f["attribute"] == "GPU" and "Mali-G52" in f["value"] for f in facts)
    plain = extract_spec_facts_from_text(text, 1, "RK3568", "d1")
    assert all(f["attribute"] != "GPU" for f in plain)


def test_extract_no_vocabulary_returns_empty():
    """Without industry-pack vocabulary the assertion layer must not extract
    structural noise from non-spec documents (annual reports etc.), which
    would otherwise be injected into queries as authoritative facts."""
    from core.ingestion.spec_facts_extractor import extract_spec_facts_from_text
    text = "营业收入（千元）\n456,451,731\n会议审议通过《2025 年第三季度报告》\n- 5亿用户\nPage Number: 49"
    facts = extract_spec_facts_from_text(text, 9, "midea", "doc1", extraction=None)
    assert facts == []
    facts = extract_spec_facts_from_text(text, 9, "midea", "doc1", extraction={})
    assert facts == []
    facts = extract_spec_facts_from_text(text, 9, "midea", "doc1",
                                         extraction={"spec_headers": [], "compute_units": [], "frequency_terms": []})
    assert facts == []


# ---- MuPDF fallback preserves page boundaries ----
def test_pymupdf_fallback_keeps_page_boundaries(monkeypatch):
    """Corrupted PDFs (pdfplumber/pypdf reject) fall back to MuPDF extraction.
    The fallback must return one entry per page — merging all pages into a
    single record would disable page-level retrieval and structure indexing
    for exactly the documents that need the fallback most."""
    import sys
    import types

    class _FakePage:
        def __init__(self, text):
            self._text = text

        def get_text(self):
            return self._text

    class _FakeDoc:
        page_count = 3

        def __getitem__(self, i):
            return _FakePage(["page one text", "", "page three text"][i])

        def close(self):
            pass

    fake_fitz = types.ModuleType("fitz")
    fake_fitz.open = lambda path: _FakeDoc()
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)

    from core.ingestion.parser import DocumentParser
    pages = DocumentParser._extract_pages_with_pymupdf("dummy.pdf")
    # empty page dropped, order preserved
    assert pages == ["page one text", "page three text"]
