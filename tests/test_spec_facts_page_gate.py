"""F-13b: pack-driven page-level relevance gate for spec-fact extraction.

TRM register-description chapters are almost entirely bit-field rows
("b0: 0x0 R/W", "Value After Reset: 0x0"). Mined as facts they carry no spec
meaning for "what does this chip support" queries, yet they inflate the index
and crowd out the injected fact quota — 19,414 of one tenant's ~35.9k facts.

Which pages to skip is domain vocabulary, so it lives in the pack
(spec_extraction.page_skip_patterns), not in core. Core keeps the mechanism:
a page whose cleaned text matches any (pattern, min_hits) entry is skipped
whole. There is deliberately NO attribute-name blacklist — bit-field names are
unbounded (b0..b31, h0..h15, vendor-specific) and an enumeration would miss the
next vendor's scheme. The gate keys on the *structure* of the page instead.

Opt-in by construction: a pack with vocabulary but no page_skip_patterns skips
nothing, so existing corpora are unaffected.
"""
from pathlib import Path

import yaml

from core.ingestion.spec_facts_extractor import (
    _build_page_skip_re,
    _page_is_skipped,
    extract_spec_facts_from_text,
)

_RULES = (Path(__file__).resolve().parent.parent
          / "industries/sample_semiconductor/retrieval/rules.yaml")

# A register page: many bit-field rows + reset-value rows.
REGISTER_PAGE = "\n".join(
    [f"b{i}: 0x0 R/W bit {i} control" for i in range(16)]
    + ["RegisterName: CRU_CLKSEL_CON0", "DefaultValue: 0x00010001",
       "Value After Reset: 0x0"])

# A genuine spec page: must never be skipped.
SPEC_PAGE = (
    "GPU\n Mali-G52 1-Core-2EE\n"
    "NPU: 6 TOPS\n"
    "Max CPU frequency 2.4 GHz\n"
    "Support 8 camera interfaces\n"
)


def _pack():
    return yaml.safe_load(_RULES.read_text(encoding="utf-8"))["spec_extraction"]


# ── the pack ships a gate and it fires on register pages ─────────────────────

def test_pack_declares_page_skip_patterns():
    assert _pack().get("page_skip_patterns"), \
        "the semiconductor pack must declare register-page skip patterns"


def test_register_page_is_skipped():
    facts = extract_spec_facts_from_text(
        REGISTER_PAGE, 1, "T536", "d1", extraction=_pack())
    assert facts == [], f"register page produced spec facts: {facts!r}"


def test_short_register_fragment_is_not_skipped():
    """min_hits protects against skipping a page that merely mentions a bit."""
    frag = "b0: 0x0 R/W\nb1: 0x1 R/W"
    facts = extract_spec_facts_from_text(
        frag, 1, "T536", "d1", extraction=_pack())
    assert facts, "a two-row fragment is not a register page"


# ── the gate must not touch genuine spec pages ───────────────────────────────

def test_spec_page_is_untouched():
    facts = extract_spec_facts_from_text(
        SPEC_PAGE, 1, "RK3588", "d1", extraction=_pack())
    attrs = {f["attribute"] for f in facts}
    assert "compute power" in attrs
    assert "Max CPU frequency" in attrs
    assert any("count" in a for a in attrs)


# ── opt-in: no patterns -> no skipping ───────────────────────────────────────

def test_gate_is_inert_without_patterns():
    """A vocabulary pack with no page gate must not skip anything."""
    ex = {"spec_headers": ["gpu"], "compute_units": ["TOPS"],
          "compute_attribute": "compute power"}
    facts = extract_spec_facts_from_text(
        REGISTER_PAGE, 1, "T536", "d1", extraction=ex)
    assert facts, "without page_skip_patterns the gate must be a no-op"


def test_no_pack_skips_nothing():
    assert extract_spec_facts_from_text(REGISTER_PAGE, 1, "T536", "d1") == []


# ── mechanism unit tests ─────────────────────────────────────────────────────

def test_build_page_skip_re_ignores_bad_patterns():
    rx = _build_page_skip_re(["valid", "([unclosed", "", None, 42])
    assert len(rx) == 1, "only the valid pattern compiles"


def test_min_hits_threshold_is_respected():
    pats = [{"pattern": r"b\d+: 0x", "min_hits": 3}]
    rx = _build_page_skip_re(pats)
    assert _page_is_skipped("b0: 0x\nb1: 0x", pats, rx) is False   # 2 < 3
    assert _page_is_skipped("b0: 0x\nb1: 0x\nb2: 0x", pats, rx) is True


def test_empty_patterns_never_skip():
    assert _build_page_skip_re([]) == []
    # Deliberate robustness check: extraction.get(...) or [] can yield None.
    assert _build_page_skip_re(None) == []  # type: ignore[arg-type]
    assert _page_is_skipped("anything", [], []) is False
