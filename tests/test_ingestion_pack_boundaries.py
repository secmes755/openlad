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


def test_summary_prompts_use_pack_templates():
    from core.plugins import get_generic_ingestion_prompts
    prompts = get_generic_ingestion_prompts()
    for key in ("chapter_summary", "document_summary", "title_extraction"):
        assert prompts.get(key), f"pack must declare {key}"
    # Templates with literal braces must be format-safe.
    prompts["title_extraction"].format(
        filename="f", summary="s", categories="c", doc_type_hint="")
    chart = prompts["chapter_summary"].format(title="T", content="C")
    assert "\u8bf7\u7528\u4e2d\u6587" not in chart
    assert "same language as the chapter content" in chart
    doc = prompts["document_summary"].format(
        filename="f.pdf", chapters="A", preview="B")
    assert "same language as the document content" in doc


def test_heading_style_markers_come_from_pack():
    from core.plugins import get_generic_ingestion_rules
    markers = get_generic_ingestion_rules().get("heading_style_markers") or []
    assert markers
    lowered = [str(m).lower() for m in markers]
    # Case-insensitive match covers the Word "Heading 1" style family.
    assert any(m in "heading 1" for m in lowered)
    assert any(m in "标题 1" for m in lowered)
    assert not any(m in "normal" for m in lowered)


def test_generic_lookup_ignores_localised_names():
    """The base pack is identified by id, never by a translated display name."""
    from core.plugins import PluginRegistry
    registry = PluginRegistry.__new__(PluginRegistry)
    registry._plugins = {"finance": object()}
    assert registry.get_generic() is None


def test_chart_vocabulary_comes_from_pack():
    from core.ingestion.layout.chart_analyzer import ChartAnalyzer
    from core.plugins import get_generic_ingestion_rules
    rules = get_generic_ingestion_rules()
    assert rules.get("table_caption_keywords")
    assert rules.get("non_chart_markers")
    assert rules.get("chart_type_labels")

    chart = ChartAnalyzer.__new__(ChartAnalyzer)
    chart._table_caption_keywords = tuple(
        str(k).lower() for k in rules["table_caption_keywords"])
    chart._non_chart_markers = tuple(str(m) for m in rules["non_chart_markers"])
    chart._chart_type_labels = tuple(str(m) for m in rules["chart_type_labels"])

    assert any(kw in "\u8868 3 \u5f15\u811a" for kw in chart._table_caption_keywords)
    assert any(kw in "table 3 pins" for kw in chart._table_caption_keywords)
    assert not any(kw in "figure 3" for kw in chart._table_caption_keywords)


def test_heading_and_table_rules_come_from_pack():
    from core.ingestion.builder import DocumentIndexBuilder
    builder = object.__new__(DocumentIndexBuilder)
    from core.plugins import get_generic_ingestion_rules
    rules = get_generic_ingestion_rules()
    builder._heading_numerals = {
        str(k): v for k, v in (rules.get("heading_numerals") or {}).items()}
    builder._heading_levels = {
        str(k): v for k, v in (rules.get("heading_levels") or {}).items()}
    builder._default_heading_level = int(
        rules.get("default_heading_level", 2) or 2)

    assert builder._heading_number_value("\u4e09") == 3
    assert builder._heading_number_value("\u5341") == 10
    assert builder._heading_number_value("\u767e") == 0  # undeclared -> 0
    assert builder._heading_level_for_unit("\u7ae0") == 2
    assert builder._heading_level_for_unit("\u8282") == 3
    # Undeclared unit falls back to the pack default, not a hardcoded value.
    assert builder._heading_level_for_unit("\u5377") == 2

    import re
    marker = rules.get("table_marker_pattern")
    caption = rules.get("table_caption_pattern")
    assert marker and caption
    assert re.search(marker, "[\u8868 3] \u5f15\u811a\u5217\u8868")
    assert re.search(caption, "[\u8868 3] \u5f15\u811a\u5217\u8868").group(1) == "\u5f15\u811a\u5217\u8868"
    assert re.search(marker, "[Table 4] Pin map")
    # A plain sentence is not a table marker.
    assert not re.search(marker, "\u666e\u901a\u6b63\u6587")
