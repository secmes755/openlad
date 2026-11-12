"""Ingestion vocabulary lives in packs; core retains only matching mechanics."""
from core.ingestion.classifier import _unknown_labels
from core.ingestion.layout.layout_analyzer import LayoutAnalyzer, LayoutElement
from core.ingestion.preprocessing.ocr_engine import TextQualityChecker
from core.plugins import get_generic_ingestion_prompts, get_generic_ingestion_rules


def test_generic_pack_declares_ingestion_rules_and_prompts():
    rules = get_generic_ingestion_rules()
    prompts = get_generic_ingestion_prompts()
    assert rules.get("unknown_labels")
    assert rules.get("quality_common_words", {}).get("zh")
    assert prompts.get("classify_system")
    assert prompts.get("classify_user")


def test_unknown_labels_normalised_from_pack():
    labels = _unknown_labels()
    assert "unknown" in labels
    assert "未知" in labels


def test_ocr_quality_dictionary_comes_from_pack():
    checker = TextQualityChecker()
    assert checker._common_words
    assert "的" in checker._common_words


def test_layout_captions_and_page_types_still_detected():
    analyzer = LayoutAnalyzer()
    # Caption patterns are compiled from the pack. (Short captions are still
    # classified as titles by the pre-existing ordering; see baseline note.)
    assert analyzer._caption_patterns
    assert analyzer._caption_patterns[0].match("图 3 系统框图")
    # Pack regexes carry their own case-insensitivity after compilation.
    assert any(p.match("table 2 pins") for p in analyzer._caption_patterns)

    toc = [LayoutElement(element_type="text", content=c, bbox=[0, 0, 1, 1])
           for c in ("目录", "1. 概述", "2. 接口", "3. 电气参数")]
    assert analyzer._detect_page_type(toc) == "toc"

    appendix = [LayoutElement(element_type="text", content="Appendix A",
                              bbox=[0, 0, 1, 1])]
    assert analyzer._detect_page_type(appendix) == "appendix"


def test_layout_without_pack_rules_degrades_safely(monkeypatch):
    monkeypatch.setattr(
        "core.plugins.get_generic_ingestion_rules", lambda: {})
    analyzer = LayoutAnalyzer()
    assert analyzer._caption_patterns == []
    assert analyzer._page_type_patterns == {}
    assert analyzer._matches_page_type("cover", "cover page") is False


def test_title_extraction_uses_pack_template(monkeypatch):
    from core.ingestion import title_deriver

    captured = {}

    class Model:
        def generate_json(self, prompt, max_tokens=256, temperature=0):
            captured['prompt'] = prompt
            return {"subject": "Alpha", "year": "2025", "doc_type": "datasheet"}

    title = title_deriver.generate_identifiable_title(
        Model(), "alpha_2025_datasheet.pdf",
        "Alpha 2025 datasheet covering interfaces and power requirements.", {})
    assert title == "Alpha 2025 datasheet"
    assert "Filename: alpha_2025_datasheet.pdf" in captured['prompt']
    assert prompt_is_pack_template(captured['prompt'])


def prompt_is_pack_template(prompt: str) -> bool:
    from core.plugins import get_generic_ingestion_prompts
    template = get_generic_ingestion_prompts().get("title_extraction", "")
    # The rendered prompt must follow the pack template's first line, not the
    # hardcoded fallback wording.
    return bool(template) and template.strip().split("\n")[0].strip() in prompt
