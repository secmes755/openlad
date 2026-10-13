"""F-13a: HTML/CSS markup in the text layer must not become spec facts.

Documents converted from HTML (or authored in rich text) keep their markup in
the PDF text layer: ``<td style="width: 40%">``, ``<div style="font-size: 1.5em">``.
The key-value pattern reads the CSS declarations as assertions, producing facts
whose attribute is a CSS property ("width", "font-size", "left") and whose
source_text is raw markup — 2,032 such rows in the reporting tenant.

The strip is text-preserving: ``<td style="width: 5%">24</td>`` -> ``24``. It
must NOT swallow legitimate ``<`` (comparison operators, subscript markup like
``T<sub>j</sub>`` which becomes ``Tj``). Real HTML->PDF text layers also
truncate tags mid-way at page breaks (``<div style="left: 1240%`` with no
closing ``>``), so the stripper recognises a tag by its element name alone.
"""
from pathlib import Path

import yaml

from core.ingestion.spec_facts_extractor import (
    extract_spec_facts_from_text,
    strip_html_scaffolding,
)

_RULES = (Path(__file__).resolve().parent.parent
          / "industries/sample_semiconductor/retrieval/rules.yaml")


def _pack():
    return yaml.safe_load(_RULES.read_text(encoding="utf-8"))["spec_extraction"]


# ── the CSS noise class must be eliminated ───────────────────────────────────

CSS_NOISE_FRAGMENTS = [
    '<td style="width: 40%; text-align: center; font-weight: bold',
    '<div style="position: absolute; top: 20px; left: 1240%; width: 80%',
    '<div style="text-align: center; font-size: 1.5em; font-weight: bold',
    '<td style="width: 5%;">24</td>',
    '<div style="line-height: 1.5; margin-top: 10px; padding: 5px">',
    '<span style="position: absolute; right: 0; bottom: 10px">',
]


def test_css_declarations_never_become_facts():
    for frag in CSS_NOISE_FRAGMENTS:
        facts = extract_spec_facts_from_text(
            frag, 1, "", "d1", extraction=_pack())
        assert facts == [], \
            f"CSS markup produced facts: {facts!r} for {frag!r}"


def test_css_attribute_names_are_not_attributes():
    """Belt and braces: no fact may carry a bare CSS property as its attribute."""
    from core.ingestion.spec_facts_extractor import _CSS_RUN_RE, _HTML_TAG_NAMES
    banned = {"width", "height", "top", "left", "right", "bottom", "position",
              "font-size", "line-height", "margin-top", "padding"}
    for frag in CSS_NOISE_FRAGMENTS:
        facts = extract_spec_facts_from_text(
            frag, 1, "", "d1", extraction=_pack())
        for f in facts:
            assert f["attribute"].lower() not in banned, f
    assert _CSS_RUN_RE and _HTML_TAG_NAMES  # referenced to keep imports honest


# ── text inside markup is preserved ──────────────────────────────────────────

def test_inner_text_is_kept():
    assert strip_html_scaffolding('<td style="width: 5%;">24</td>') == "24"
    assert strip_html_scaffolding("<div class='x'>Total</div>") == "Total"
    assert strip_html_scaffolding("<tr><td>a</td><td>b</td></tr>") == "ab"


def test_known_tag_with_real_content_still_extracts():
    """A table cell carrying a real spec must survive the strip."""
    facts = extract_spec_facts_from_text(
        '<div class="spec">ball size: 0.35mm</div>', 1, "X", "d1",
        extraction=_pack())
    assert any(f["attribute"] == "ball size" and f["value"] == "0.35mm"
               for f in facts), facts


# ── boundary: legitimate "<" must survive ────────────────────────────────────

def test_subscript_markup_collapses_to_text():
    assert strip_html_scaffolding("T<sub>j</sub>") == "Tj"
    facts = extract_spec_facts_from_text(
        "T<sub>j</sub>: 125 C", 1, "X", "d1", extraction=_pack())
    assert any(f["attribute"] == "Tj" and f["value"] == "125 C"
               for f in facts), facts


def test_bare_less_than_is_not_touched():
    assert strip_html_scaffolding("a < b and c > d") == "a < b and c > d"
    assert strip_html_scaffolding("VDD < 3.3 V") == "VDD < 3.3 V"


def test_truncated_tag_at_page_break_is_stripped():
    """A page-break-cut tag has neither '>' nor a closing quote."""
    assert strip_html_scaffolding('<div style="left: 1240%') == ""


def test_none_and_empty_are_safe():
    assert strip_html_scaffolding("") == ""
    # Deliberate robustness check: page text can be None upstream.
    assert strip_html_scaffolding(None) == ""  # type: ignore[arg-type]
    assert extract_spec_facts_from_text("", 1, "X", "d1",
                                        extraction=_pack()) == []
