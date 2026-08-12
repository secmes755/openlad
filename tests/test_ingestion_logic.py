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
def test_extract_spec_facts_support_sentence():
    facts = extract_spec_facts_from_text(
        "The chip supports ten UART interfaces.", 1, "RK3588", "d1")
    # attribute keeps the surface case of the feature word (not lowercased)
    assert any(f["attribute"] == "UART interfaces count" and f["value"] == "10"
               for f in facts)


def test_extract_spec_facts_key_value():
    facts = extract_spec_facts_from_text(
        "ball size: 0.35mm\nball pitch: 0.65mm", 1, "RK3588", "d1")
    assert any(f["attribute"] == "ball size" and f["value"] == "0.35mm"
               for f in facts)


def test_extract_spec_facts_skips_vlm_hallucinations():
    raw = ("Real spec line: ball size: 0.35mm\n"
           "--- Page Visual Analysis (VLM)\n"
           "The chip has 560 solder balls.")
    facts = extract_spec_facts_from_text(raw, 1, "RK3588", "d1")
    assert any(f["value"] == "0.35mm" for f in facts)
    assert all(f["value"] != "560" for f in facts)
