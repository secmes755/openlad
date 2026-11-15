"""Single-document FTS must use the same pack vocabulary as exact retrieval."""
from types import SimpleNamespace

from core.retrieval.executor import RetrievalExecutor
from core.retrieval.retriever import HierarchicalRetriever


def test_prepare_fts_query_keeps_original_and_appends_pack_terms():
    retriever = HierarchicalRetriever.__new__(HierarchicalRetriever)
    retriever._tokenize_query = lambda query: ["营业收入"]
    retriever._expand_query_terms = lambda keywords, query, routed_category=None: keywords + ["total revenue"]
    fts_query, tokens = retriever._prepare_fts_query("年报 营业收入")
    assert fts_query == "年报 营业收入 total revenue"
    assert tokens == ["营业收入", "total revenue"]


def test_single_document_fallback_uses_expanded_query(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        HierarchicalRetriever, "_prepare_fts_query",
        lambda self, query, routed_category=None: (query + " total revenue", ["total revenue"]),
    )
    def fake_search(self, query, **kwargs):
        captured.setdefault("queries", []).append(query)
        return []
    monkeypatch.setattr(HierarchicalRetriever, "_search_fts", fake_search)
    executor = RetrievalExecutor.__new__(RetrievalExecutor)
    executor.tenant_id = "benchmark"
    executor.metadata_db = SimpleNamespace(get_document=lambda doc_id: {})
    assert executor._fallback_to_fts("年报 营业收入", "doc") == []
    # The original query is never rewritten in place; pack terms are searched
    # as separate routes and fused, so whichever route runs, the query text is
    # either the caller's own or a pack term -- never a concatenation.
    assert "total revenue" in captured["queries"]
    assert "年报 营业收入" in captured["queries"]


def test_executor_uses_planner_category_unless_pack_is_explicit():
    for hint, expected in [("auto", "财务报告"), ("sample_semiconductor", "sample_semiconductor")]:
        executor = RetrievalExecutor.__new__(RetrievalExecutor)
        captured = {}
        def fake_standard(steps, **kwargs):
            captured["industry"] = kwargs["industry_hint"]
            return {}
        executor._execute_standard = fake_standard
        executor.execute(
            {"strategy": "single_retrieve", "steps": [], "routed_category": "财务报告"},
            industry_hint=hint,
        )
        assert captured["industry"] == expected


def test_synonym_searches_use_rank_fusion():
    retriever = HierarchicalRetriever.__new__(HierarchicalRetriever)
    retriever._prepare_fts_query = lambda query, category=None: (
        query, ["营业收入", "revenue", "total revenue"])
    retriever._tokenize_query = lambda query: ["营业收入"]
    def fake_search(query, **kwargs):
        if query == "total revenue":
            return [{"page_id": "answer", "score": 10.0}]
        if query == "revenue":
            return [{"page_id": "noise", "score": 1000.0}, {"page_id": "answer", "score": 900.0}]
        return []
    retriever._search_fts = fake_search
    rows = retriever._search_fts_with_pack_expansion("年报 营业收入")
    assert [row["page_id"] for row in rows] == ["answer", "noise"]
    assert rows[0]["score"] > rows[1]["score"]


def test_merger_loads_pack_section_boost_rules(monkeypatch):
    from core import plugins
    boost = {"total": {
        "query_keywords": ["metric"],
        "boost_sections": [{"keywords": ["Total Table"], "boost": 2.5}],
    }}
    retrieval = SimpleNamespace(
        get_low_value_sections=lambda: [],
        get_section_boost_rules=lambda: boost,
    )
    registry = SimpleNamespace(
        get_plugin=lambda name: SimpleNamespace(retrieval=retrieval),
        get_plugin_by_category=lambda name: None,
    )
    monkeypatch.setattr(plugins, "get_plugin_registry", lambda: registry)
    from core.retrieval.retriever import SegmentMerger
    merger = SegmentMerger.__new__(SegmentMerger)
    rules = merger._load_industry_boost_rules("category")
    assert rules["total"] == boost["total"]
    assert merger._match_boost_rule("metric", "Total Table", rules) == 2.5


def test_synonym_group_is_reachable_through_any_member():
    """A query phrased with a non-head member must reach the whole group."""
    from core.plugins import get_plugin_registry
    retriever = HierarchicalRetriever.__new__(HierarchicalRetriever)
    retriever._tokenize_query = lambda q: ["归母净利润"]
    plugin = SimpleNamespace(retrieval=SimpleNamespace(
        get_spec_query_terms=lambda: {"净利润": ["归母净利润", "net profit"]},
    ))
    registry = get_plugin_registry()
    original = registry.get_plugin
    registry.get_plugin = lambda name: plugin
    try:
        terms = retriever._expand_query_terms(["归母净利润"], "归母净利润",
                                              "财务报告")
    finally:
        registry.get_plugin = original
    assert "net profit" in terms
    assert "净利润" in terms


def test_single_route_precision_beats_broad_consensus():
    """A page top-ranked by one route must beat a page only middling on many."""
    retriever = HierarchicalRetriever.__new__(HierarchicalRetriever)
    retriever._prepare_fts_query = lambda q, c=None: (q, ["q", "exact", "broad1", "broad2"])
    retriever._tokenize_query = lambda q: ["q"]
    target = {"page_num": 99, "page_id": "p99", "score": 1.0}
    def fake_search(query, **kwargs):
        if query == "exact":
            return [target]
        if query in ("broad1", "broad2"):
            filler = [{"page_num": n, "page_id": f"p{n}", "score": 1.0}
                      for n in range(1, 6)]
            return filler + [target]
        return []
    retriever._search_fts = fake_search
    rows = retriever._search_fts_with_pack_expansion("q")
    assert rows[0]["page_num"] == 99
