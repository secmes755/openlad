"""Annex headings must reach the structure index.

Standards documents carry their appendices outside the numbered clause tree,
as "Annex A" .. "Annex T". The text-rule builder only recognised numeric
clauses and Chapter/Section/Part headings, so every annex page was filed under
whatever chapter came before it. A question about an annex then had no
structure anchor and retrieval landed on the table of contents, where the
annex is merely listed, instead of the annex body.

"Annex X" alone is not proof of a heading: interface tables mention several
annexes in one cell. The builder therefore requires the (normative) or
(informative) marker, inline or within the next two lines.
"""
from core.ingestion.builder import DocumentIndexBuilder


def _builder():
    """A builder without the model/parser stack: the structure rules are pure."""
    return DocumentIndexBuilder.__new__(DocumentIndexBuilder)


def _structure(pages):
    return _builder()._build_structure_from_text("d1", {
        num: {"page_text": text} for num, text in pages.items()
    })


def test_marker_on_following_line_is_a_heading():
    si = _structure({189: "Annex H\n(normative)\nFunctional safety requirements\nH.1 General"})
    assert si[189]["path"] == "Annex H"
    assert si[189]["title"] == "Functional safety requirements"


def test_inline_marker_is_a_heading():
    si = _structure({176: "Annex A (normative) Scope of testing"})
    assert si[176]["path"] == "Annex A"
    assert si[176]["title"] == "Scope of testing"


def test_marker_after_title_is_a_heading():
    si = _structure({180: "Annex C\nTest equipment\n(informative)"})
    assert si[180]["path"] == "Annex C"
    assert si[180]["title"] == "Test equipment"


def test_annex_without_title_still_records_a_path():
    si = _structure({182: "Annex D\n(normative)"})
    assert si[182]["path"] == "Annex D"


def test_annex_with_chinese_title():
    si = _structure({184: "Annex E 关于安全的一般要求\n(normative)"})
    assert si[184]["path"] == "Annex E"
    assert si[184]["title"] == "关于安全的一般要求"


def test_body_reference_without_marker_is_not_a_heading():
    """'See Annex K for details' is prose, not a heading."""
    si = _structure({170: "Annex A\n(normative)\nScope", 172: "See Annex K for details"})
    assert si[172]["path"] == "Annex A", "a prose reference must not start a new section"


def test_table_cell_listing_several_annexes_is_not_a_heading():
    si = _structure({160: "Annex A\n(normative)\nScope",
                     200: "Physical layer | Annex M / Annex N | optional"})
    assert si[200]["path"] == "Annex A", "a table cell is not a heading"


def test_numbered_clauses_still_work():
    si = _structure({10: "3.4.5 Protection against overvoltage\nBody text."})
    assert si[10]["path"] == "3.4.5"
