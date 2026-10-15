"""The merge phase must receive the rule shape it actually reads.

``SegmentMerger._load_industry_boost_rules`` returned
``plugin.retrieval.get_section_boost_rules()``, whose keys come from the
``section_boost_rules`` block of ``rules.yaml``. That block is a *sibling* of
``low_value_sections``, not a container of it, so the merge phase's
``if "low_value_sections" in industry_rules`` was never true and the low-value
section penalty silently never applied. The accessor that returns the right block
(``get_low_value_sections``) already existed on the plugin.
"""
from unittest.mock import MagicMock

from core.retrieval.retriever import SegmentMerger


def _merger():
    m = SegmentMerger.__new__(SegmentMerger)
    m.tenant_id = None  # type: ignore[assignment]  # matches the __init__ default
    m.metadata_db = None
    return m


def test_the_merge_phase_key_is_present(monkeypatch):
    """The key the merge phase reads must come back at the top level."""
    plugin = MagicMock()
    plugin.retrieval.get_low_value_sections.return_value = [
        {"keywords": ["封面", "目录"], "penalty": 3.0}
    ]
    registry = MagicMock()
    registry.get_plugin.return_value = plugin
    monkeypatch.setattr("core.plugins.get_plugin_registry", lambda: registry)

    rules = _merger()._load_industry_boost_rules("sample_semiconductor")

    assert "low_value_sections" in rules
    assert rules["low_value_sections"] == [{"keywords": ["封面", "目录"], "penalty": 3.0}]


def test_the_real_pack_yields_a_non_empty_rule_list(monkeypatch):
    """Against the shipped pack: the list is non-empty, so the bug was observable."""
    rules = _merger()._load_industry_boost_rules("sample_semiconductor")
    sections = rules.get("low_value_sections")
    assert sections, "the shipped pack declares low_value_sections rules"
    assert any(rule.get("keywords") for rule in sections)


def test_merge_penalty_keywords_are_actually_collected():
    """Reproduces the merge phase read (retriever.py:1443-1447)."""
    merger = _merger()
    generic_indicators = ["cover page"]
    low_value_keywords = list(generic_indicators)

    industry_rules = merger._load_industry_boost_rules("sample_semiconductor")
    if industry_rules and "low_value_sections" in industry_rules:
        for rule in industry_rules.get("low_value_sections", []):
            low_value_keywords.extend(rule.get("keywords", []))

    assert len(low_value_keywords) > len(generic_indicators), (
        "industry keywords must be added; before the fix they never were"
    )


def test_auto_and_empty_hints_stay_empty(monkeypatch):
    """'auto' deliberately applies no pack rules; that behaviour is unchanged."""
    for hint in ("auto", "", None):
        assert _merger()._load_industry_boost_rules(hint) == {}


def test_an_unloadable_pack_is_not_fatal(monkeypatch):
    registry = MagicMock()
    registry.get_plugin.side_effect = RuntimeError("boom")
    monkeypatch.setattr("core.plugins.get_plugin_registry", lambda: registry)
    assert _merger()._load_industry_boost_rules("sample_semiconductor") == {}
