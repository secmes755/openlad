"""Spec-fact presentation and evidence appendix checks (pure logic, no LLM/DB).

Covers the two presentation modes of format_spec_facts and the deterministic
post-answer evidence appendix (verbatim source excerpts with citations).
"""
import pytest

from core.config import settings
from core.retrieval.spec_facts import (
    _trim_verbatim,
    build_evidence_appendix,
    format_spec_facts,
)

_PCIE_FACT = {
    "entity": "RK3568",
    "attribute": "PCIe protocol",
    "value": "PCIe3.1(8Gbps), PCIe2.1",
    "unit": "",
    "page_num": 16,
    "doc_id": "docA",
    "source_text": ("Support PCIe3.1(8Gbps) protocol and backward compatible "
                    "with the PCIe2.1 and PCIe1.1"),
}


@pytest.fixture
def _restore_config():
    snapshot = dict(settings.CONTEXT_CONFIG)
    yield
    settings.CONTEXT_CONFIG.clear()
    settings.CONTEXT_CONFIG.update(snapshot)


def test_format_source_first_hides_value_enumeration(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_presentation"] = "source_first"
    block = format_spec_facts([_PCIE_FACT])
    assert "Support PCIe3.1(8Gbps) protocol and backward compatible" in block
    assert "PCIe protocol" in block and "page 16" in block
    # the flattened enumeration must not anchor the model's reading
    assert "PCIe protocol: PCIe3.1(8Gbps), PCIe2.1" not in block
    assert "[RK3568]" in block  # entity grouping kept


def test_format_value_first_legacy(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_presentation"] = "value_first"
    block = format_spec_facts([_PCIE_FACT])
    assert "PCIe protocol: PCIe3.1(8Gbps), PCIe2.1" in block
    assert "原文:" in block


def test_format_source_first_falls_back_when_no_source(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_presentation"] = "source_first"
    fact = dict(_PCIE_FACT, source_text="")
    block = format_spec_facts([fact])
    assert "PCIe protocol: PCIe3.1(8Gbps), PCIe2.1" in block


def test_appendix_zh_verbatim_with_citation(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_evidence_appendix"] = True
    out = build_evidence_appendix([_PCIE_FACT], doc_titles={"docA": "RK3568"},
                                  query_text="RK3568支持PCIe 3.0吗")
    assert "依据原文" in out
    assert "《RK3568》第 16 页" in out
    assert "Support PCIe3.1(8Gbps) protocol and backward compatible" in out


def test_appendix_en_header(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_evidence_appendix"] = True
    out = build_evidence_appendix([_PCIE_FACT], doc_titles={"docA": "RK3568"},
                                  query_text="Does RK3568 support PCIe 3.0?")
    assert "Source excerpts:" in out
    assert "RK3568, p.16" in out


def test_appendix_disabled_by_switch(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_evidence_appendix"] = False
    assert build_evidence_appendix([_PCIE_FACT], query_text="中文") == ""


def test_appendix_skips_facts_without_source_and_caps(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_evidence_appendix"] = True
    settings.CONTEXT_CONFIG["spec_facts_evidence_max"] = 2
    facts = [dict(_PCIE_FACT, source_text=""),
             dict(_PCIE_FACT, source_text="Sentence one.", page_num=1),
             dict(_PCIE_FACT, source_text="Sentence two.", page_num=2),
             dict(_PCIE_FACT, source_text="Sentence three.", page_num=3)]
    out = build_evidence_appendix(facts, query_text="中文")
    assert "Sentence one." in out and "Sentence two." in out
    assert "Sentence three." not in out  # capped at 2


def test_appendix_dedupes_same_source(_restore_config):
    settings.CONTEXT_CONFIG["spec_facts_evidence_appendix"] = True
    dup = dict(_PCIE_FACT, attribute="PCIe controller count", value="2")
    out = build_evidence_appendix([_PCIE_FACT, dup], query_text="中文")
    assert out.count("Support PCIe3.1") == 1


def test_trim_verbatim_sentence_boundary():
    text = "First sentence. Second sentence is here. " + "x" * 400
    out = _trim_verbatim(text, cap=60)
    assert out == "First sentence. Second sentence is here."
    long_no_break = "x" * 400
    assert _trim_verbatim(long_no_break, cap=60).endswith("…")
    assert _trim_verbatim("short", cap=60) == "short"
