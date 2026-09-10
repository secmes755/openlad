"""Contract tests for RetrievalExecutor.

Written as the net for refactoring the 611-line ``_retrieve_exact``: these pin
what callers observe — the shape of ``execute()``'s result, which strategy each
plan shape dispatches to, the per-request tenant switch, and the quota
arithmetic — without depending on retrieval quality.

``_execute_step`` and ``merger`` are replaced with fakes; everything else runs for
real (the dispatch, the quota maths, the tracing, the truncation and the result
assembly).
"""
from types import SimpleNamespace

from core.retrieval.executor import RetrievalExecutor


def _executor(monkeypatch, max_chars: int = 100_000, step_results=None, merge_chars: int = 50):
    ex = RetrievalExecutor(tenant_id="admin")
    ex.max_chars = max_chars
    monkeypatch.setattr(ex, "_resolve_doc_filter", lambda doc_filter, silent=False: list(doc_filter or []))

    def fake_step(tool, query, resolved_filter, purpose, quota,
                  original_query=None, industry_hint=None):
        return list(step_results or [])

    def fake_merge(results, max_context_chars=None, query=None, industry_hint=None):
        if not results:
            return "", []
        size = min(merge_chars, max_context_chars) if max_context_chars else merge_chars
        return "x" * size, [{"doc_id": "d1", "title": "T", "pages": []}]

    monkeypatch.setattr(ex, "_execute_step", fake_step)
    ex.merger = SimpleNamespace(merge=fake_merge)
    return ex


# ── dispatch ──────────────────────────────────────────────────────────────────

def test_execute_dispatches_each_strategy(monkeypatch):
    ex = _executor(monkeypatch)
    seen = []

    def decomposed(steps, original_query=None, industry_hint=None):
        seen.append("decomposed")
        return {"strategy": "decomposed_retrieve"}

    def standard(steps, strategy_label="single_retrieve", original_query=None, industry_hint=None):
        seen.append("standard")
        return {"strategy": strategy_label}

    monkeypatch.setattr(ex, "_execute_decomposed", decomposed)
    monkeypatch.setattr(ex, "_execute_standard", standard)

    assert ex.execute({"strategy": "decomposed_retrieve", "steps": []})["strategy"] == "decomposed_retrieve"
    assert ex.execute({"strategy": "compare_docs", "steps": []})["strategy"] == "compare_docs"
    assert ex.execute({"steps": []})["strategy"] == "single_retrieve"     # default
    assert seen == ["decomposed", "standard", "standard"]


def test_execute_follows_the_tenant_and_rebuilds_its_components(monkeypatch):
    """The engine passes a different tenant per request; nothing may leak across."""
    ex = RetrievalExecutor(tenant_id="admin")
    rebuilt = []
    monkeypatch.setattr("core.retrieval.executor.HierarchicalRetriever",
                        lambda tid: rebuilt.append(("retriever", tid)) or SimpleNamespace())
    monkeypatch.setattr("core.retrieval.executor.SegmentMerger",
                        lambda tid: rebuilt.append(("merger", tid)) or SimpleNamespace())
    monkeypatch.setattr("core.retrieval.executor.get_tenant_metadata_db",
                        lambda tid: rebuilt.append(("metadata_db", tid)) or SimpleNamespace())
    monkeypatch.setattr(ex, "_execute_standard",
                        lambda steps, strategy_label="", original_query=None, industry_hint=None: {"ok": True})

    ex.execute({"steps": []}, tenant_id="acme")
    assert ex.tenant_id == "acme"
    assert rebuilt == [("retriever", "acme"), ("merger", "acme"), ("metadata_db", "acme")]

    rebuilt.clear()
    ex.execute({"steps": []}, tenant_id="acme")          # same tenant: nothing rebuilt
    assert rebuilt == []


# ── quota arithmetic ─────────────────────────────────────────────────────────

def test_quotas_are_empty_without_steps(monkeypatch):
    assert _executor(monkeypatch)._calculate_step_quotas([]) == []


def test_a_single_step_quota_respects_its_bounds(monkeypatch):
    ex = _executor(monkeypatch, max_chars=200_000)
    (quota,) = ex._calculate_step_quotas([{"doc_filter": []}])
    assert 20_000 <= quota <= 80_000, quota       # min_single_step_quota / single_step_quota_max


def test_a_multi_step_quota_total_is_capped(monkeypatch):
    ex = _executor(monkeypatch, max_chars=200_000)
    quotas = ex._calculate_step_quotas([{"doc_filter": []} for _ in range(6)])
    assert all(q > 0 for q in quotas), quotas
    assert sum(quotas) <= 80_000, sum(quotas)     # multi_step_quota_max


def test_filtered_steps_share_their_allowance_by_document_count(monkeypatch):
    ex = _executor(monkeypatch, max_chars=100_000)
    quotas = ex._calculate_step_quotas([
        {"doc_filter": ["a", "b", "c"]},          # 3 documents
        {"doc_filter": ["d"]},                    # 1 document
    ])
    assert quotas[0] == 3 * quotas[1], quotas


def test_an_unfiltered_step_takes_what_is_left(monkeypatch):
    ex = _executor(monkeypatch, max_chars=100_000)
    quotas = ex._calculate_step_quotas([
        {"doc_filter": ["a", "b"]},
        {"doc_filter": []},                       # no filter: gets the remainder
    ])
    assert sum(quotas) <= 100_000
    assert all(q > 0 for q in quotas), quotas


# ── result contract ──────────────────────────────────────────────────────────

def test_execute_standard_returns_the_documented_shape(monkeypatch):
    ex = _executor(monkeypatch, step_results=[SimpleNamespace(doc_id="d1")])
    result = ex._execute_standard([{"tool": "single_retrieve", "query": "q", "purpose": "p"}],
                                 strategy_label="single_retrieve")

    assert set(result) == {"context", "sources", "trace", "total_results", "total_chars", "strategy"}
    assert result["strategy"] == "single_retrieve"
    assert result["total_results"] == 1
    assert result["total_chars"] == len(result["context"])
    assert [t["step"] for t in result["trace"]] == [1]
    assert result["trace"][0]["results_count"] == 1
    assert result["trace"][0]["context_chars"] == len(result["context"])


def test_execute_standard_without_results_still_returns_the_shape(monkeypatch):
    ex = _executor(monkeypatch, step_results=[])
    result = ex._execute_standard([{"tool": "single_retrieve", "query": "q"}])

    assert result["context"] == ""
    assert result["sources"] == []
    assert result["total_results"] == 0
    assert result["strategy"] == "single_retrieve"


def test_execute_standard_stitches_one_context_per_step(monkeypatch):
    ex = _executor(monkeypatch, max_chars=100_000,
                   step_results=[SimpleNamespace(doc_id="d1")], merge_chars=30)
    result = ex._execute_standard([
        {"tool": "single_retrieve", "query": "q1"},
        {"tool": "single_retrieve", "query": "q2"},
    ])

    assert len(result["trace"]) == 2
    assert result["total_chars"] == len(result["context"])
    assert result["total_results"] == 2
    assert result["sources"], "sources from every step must reach the caller"


def test_a_step_context_is_capped_at_its_quota(monkeypatch):
    """The merger's output is re-checked against the step quota."""
    ex = _executor(monkeypatch, max_chars=100_000,
                   step_results=[SimpleNamespace(doc_id="d1")], merge_chars=90_000)
    result = ex._execute_standard([{"tool": "single_retrieve", "query": "q"}])

    quota = result["trace"][0]["quota"]
    assert result["trace"][0]["context_chars"] <= quota, result["trace"][0]
