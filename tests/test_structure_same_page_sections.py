"""A page that opens several sections is filed under the first one.

Datasheets routinely start a new numbered section mid-page. The builder used to
take the LAST section at or before the page, so when two sections began on the
same page the earlier one was overwritten: the RK3568 "1.2.10 Graphics Engine"
page was recorded as "1.2.11 Video input interface", and its GPU content could
not be reached by searching for it.
"""
from core.ingestion.builder import DocumentIndexBuilder

PAGES = {p: {"page_text": f"page {p} body"} for p in range(9, 14)}

TOC = [
    (4, "1.2.7 Video CODEC", 9),
    (4, "1.2.8 JPEG CODEC", 10),
    (4, "1.2.9 Image Enhancement (IEP module)", 10),
    (4, "1.2.10 Graphics Engine", 11),
    (4, "1.2.11 Video input interface", 11),
    (4, "1.2.12 Display interface", 12),
]


def _structure():
    builder = DocumentIndexBuilder.__new__(DocumentIndexBuilder)
    index, _ = builder._build_structure_from_toc("d1", PAGES, TOC)
    return index


def test_page_with_two_sections_uses_the_first():
    si = _structure()
    assert si[11]["title"] == "Graphics Engine"
    assert si[11]["short_path"] == "1.2.10"


def test_later_sections_on_the_same_page_are_kept_as_metadata():
    si = _structure()
    # Not the page's subject, but not silently lost either.
    assert "Video input interface" in si[11]["additional_sections"]


def test_pages_with_a_single_section_are_unaffected():
    si = _structure()
    assert si[9]["title"] == "Video CODEC"
    assert si[12]["title"] == "Display interface"
    assert si[12]["additional_sections"] == []


def test_page_after_a_boundary_keeps_its_own_section():
    si = _structure()
    # Page 13 opens nothing, so it inherits the last section that did.
    assert si[13]["title"] == "Display interface"
