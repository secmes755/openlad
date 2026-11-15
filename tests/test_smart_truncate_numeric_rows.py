"""Text scoring alone drops table data rows, which carry numbers, not words."""
from core.retrieval.retriever import SegmentMerger


def _truncate(page, query, max_len, extra=None):
    merger = SegmentMerger.__new__(SegmentMerger)
    return merger._smart_truncate(page, query, max_len, extra_keywords=extra)


def test_numeric_row_next_to_a_matching_header_survives_truncation():
    page = "\n".join([
        "Filler paragraph that says nothing relevant at all.",
        "Operating revenue (RMB'000)",
        "372,037,280 343,917,531 8.18%",
    ] + [f"More filler line {i} with no matching words." for i in range(20)])
    # Below the render overhead nothing fits; use a budget a real page can use.
    kept = _truncate(page, "operating revenue", 400)
    assert "372,037,280" in kept


def test_numeric_rows_far_from_any_header_are_not_pulled_in():
    # The last block is always kept as page context, so put the distant numeric
    # row in the middle where only the adjacency rule could select it.
    page = "\n".join([
        "Operating revenue (RMB'000)",
    ] + [f"Neutral line {i}." for i in range(8)]
        + ["999,888,777 111,222,333"]
        + [f"Trailing filler {i}." for i in range(8)])
    full = len(page)
    kept = _truncate(page, "operating revenue", 200)
    assert len(kept) < full, "test must actually truncate"
    assert "999,888,777" not in kept
