"""Regression guards for the 0.5.1 silent-failure fixes.

Each of these defects had the same shape: the system kept running and produced
an answer that looked like a negative result. A prompt lost its output
contract, a candidate list parsed to empty, a table chunk outgrew the embedding
window, a merge anchor multiplied page text, a test file decoded under the
wrong locale. None of them raised.
"""
import pytest

from core.ingestion.parser import DocumentParser
from core.retrieval.planner import QueryPlanner
from core.retrieval.truncation import truncate_to_fit

# --- truncation must be able to preserve a trailing contract ----------------

def test_truncate_keeps_tail_when_asked():
    text = "A" * 200 + 'Output JSON: {"candidate_short_ids":["x"]}'
    kept = truncate_to_fit(text, 80, keep_tail=True)
    assert "candidate_short_ids" in kept


def test_truncate_default_still_keeps_head():
    text = "A" * 200 + 'Output JSON: {"candidate_short_ids":["x"]}'
    kept = truncate_to_fit(text, 80)
    assert "candidate_short_ids" not in kept
    assert kept.startswith("A")


# --- a reply is readable through any declared key spelling ------------------

@pytest.mark.parametrize("key", ["candidate_short_ids", "candidate_ids",
                                 "documents", "ids"])
def test_coarse_filter_accepts_equivalent_keys(key):
    planner = QueryPlanner.__new__(QueryPlanner)
    assert planner._extract_short_ids({key: ["a1", "b2"]}) == ["a1", "b2"]


def test_coarse_filter_reports_unusable_reply():
    planner = QueryPlanner.__new__(QueryPlanner)
    assert planner._extract_short_ids({"analysis": "no ids here"}) == []
    assert planner._extract_short_ids({}) == []
    assert planner._extract_short_ids({"documents": "not-a-list"}) == []


# --- a table row longer than the budget is split, not emitted whole --------

def test_oversized_table_row_is_split_with_header():
    parser = DocumentParser.__new__(DocumentParser)
    header = "| Port | Func | Note |\n| --- | --- | --- |"
    giant = "| PB | GPIO | " + "x" * 56000 + " |"
    chunks = parser._chunk_by_structure(header + "\n" + giant, max_chunk_size=1600)
    assert len(chunks) > 1
    # Header overhead is counted in the budget; nothing may approach the raw row.
    assert max(len(c) for c in chunks) < 2000


def test_normal_table_still_chunks_atomically():
    parser = DocumentParser.__new__(DocumentParser)
    header = "| A | B |\n| --- | --- |"
    rows = "\n".join(f"| {i} | {i * 2} |" for i in range(3))
    chunks = parser._chunk_by_structure(header + "\n" + rows, max_chunk_size=1600)
    assert len(chunks) == 1
