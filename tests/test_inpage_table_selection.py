"""In-page block selection must keep table rows that hold the answer."""
from core.retrieval.retriever import SegmentMerger

PAGE = "\n".join([
    "Quarterly Report Header",
    "Breakdown of operating revenue",
    "Unit: RMB'000",
    "Total 372,037,280 343,917,531 8.18%",
    "By business segment",
    "Manufacturing 333,060,319 305,846,997 8.90%",
] + [f"filler paragraph number {i} without any metric wording at all" for i in range(60)])


def _merge():
    merger = SegmentMerger.__new__(SegmentMerger)
    return merger


def test_pack_synonyms_let_a_translated_page_select_its_blocks():
    merger = _merge()
    without = merger._smart_truncate(PAGE, "营业收入", 400)
    with_terms = merger._smart_truncate(
        PAGE, "营业收入", 400,
        extra_keywords=["operating revenue", "total revenue"])
    assert len(with_terms) > len(without)
    assert "Breakdown of operating revenue" in with_terms


def test_numeric_table_rows_next_to_a_keyword_block_are_kept():
    merger = _merge()
    result = merger._smart_truncate(
        PAGE, "营业收入", 600,
        extra_keywords=["operating revenue"])
    assert "372,037,280" in result


def test_unrelated_numeric_rows_are_not_swept_in():
    page = "\n".join([
        "Operating revenue discussion",
        "unrelated text block",
        "unrelated text block",
        "unrelated text block",
        "unrelated text block",
        "unrelated text block",
        "12,345,678 at the very end of an unrelated appendix",
    ] + [f"tail filler {i}" for i in range(40)])
    merger = _merge()
    result = merger._smart_truncate(
        page, "营业收入", 300, extra_keywords=["operating revenue"])
    assert "Operating revenue discussion" in result
