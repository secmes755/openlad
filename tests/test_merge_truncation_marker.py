"""#20: every context cut in SegmentMerger.merge must carry the truncation marker.

synthesizer.assess_confidence downgrades answers built on cut-short context,
but only when it can SEE the cut: is_truncated() looks for the canonical
[TRUNCATED] marker (substring match). SegmentMerger.merge had two cuts that
bypassed mark_truncated(): the per-document budget cut appended a bare "..."
and the final safety truncation appended nothing — so an answer synthesized
from truncated context could still be reported as "high" confidence.

The final safety truncation is unreachable through merge()'s current
arithmetic (every append path is budget-guarded), so it is covered only by
the code fix itself; the reachable per-doc cut is tested here.
"""
from core.retrieval.retriever import SearchResult, SegmentMerger
from core.retrieval.truncation import MARKER, is_truncated


class _StubMetadataDB:
    """merge() only needs execute() (section expansion) and get_document()."""

    class _Rows:
        def fetchall(self):
            return []

    def execute(self, *a, **k):
        return self._Rows()

    def get_document(self, doc_id):
        return None


def _merger():
    m = SegmentMerger()  # tenant_id=None -> metadata_db None
    m.metadata_db = _StubMetadataDB()
    return m


def _page(doc_id, page_num, content, score=1.0):
    return SearchResult(doc_id=doc_id, page_num=page_num, score=score,
                        content=content, filename=f"{doc_id}.pdf")


# 8000 chars, no sentence ends, no blank lines: _segment_page_content passes
# it through and _smart_truncate caps it to ~6000 chars WITHOUT a marker
# (query="" -> no keyword sentences -> head/tail fit under the cap).
BIG = "abcdefgh" * 1000


def test_per_doc_budget_cut_carries_marker():
    # Two docs -> per-doc budget = max(20000 // 2, 5000) = 10000, and the
    # single-page cap clamps each page to 0.33 * 10000 = 3300 chars. Three
    # capped pages fit the doc budget (~9648); the fourth busts it while the
    # global budget still has room -> the partial-append path.
    m = _merger()
    results = [_page("d1", n, BIG) for n in (1, 2, 3, 4)]
    results.append(_page("d2", 1, "short page"))
    merged, _sources = m.merge(results, max_context_chars=20000, query="")
    assert MARKER in merged, "a cut the confidence signal cannot see"
    assert is_truncated(merged)


def test_partial_cut_stays_within_budget_including_marker():
    m = _merger()
    results = [_page("d1", n, BIG) for n in (1, 2, 3, 4)]
    results.append(_page("d2", 1, "short page"))
    merged, _sources = m.merge(results, max_context_chars=20000, query="")
    assert len(merged) <= 20000


def test_untruncated_merge_has_no_marker():
    m = _merger()
    merged, _sources = m.merge([_page("d1", 1, "short content")],
                               max_context_chars=20000, query="")
    assert not is_truncated(merged)
