"""Retrieval pipeline pure-logic checks (synthetic inputs, no LLM/DB/services)."""
from pathlib import Path
from types import SimpleNamespace

from core.config import settings
from core.retrieval.planner import QueryPlanner
from core.retrieval.retriever import HierarchicalRetriever, SegmentMerger
from core.retrieval.router import IntentRouter, IntentType, QueryPlan


def _router():
    # skip __init__ (constructs a model client) — keyword extraction is pure
    return object.__new__(IntentRouter)


def _retriever():
    # skip __init__ (touches tenant DB) — tokenization is pure
    return object.__new__(HierarchicalRetriever)


def _planner():
    # skip __init__ (touches tenant DB / model client) — stopword filter is pure
    return object.__new__(QueryPlanner)


# ---- QueryPlan.get_max_results ----
def test_max_results_default_and_custom():
    plan = QueryPlan(intent=IntentType.EXACT_LOOKUP, raw_query="q")
    assert plan.get_max_results() == 20
    assert plan.get_max_results(base_max=50) == 50


def test_max_results_deep_explore_scales_and_caps():
    plan = QueryPlan(intent=IntentType.RELATION_QUERY, raw_query="q",
                     entities=["RK3588", "RK3568", "T536"], deep_explore=True)
    assert plan.get_max_results() == 45  # 3 entities * multiplier 15
    plan.entities = [f"e{i}" for i in range(40)]
    assert plan.get_max_results() == 500  # capped at DEEP_EXPLORE_MAX_RESULTS


# ---- IntentRouter.extract_search_keywords ----
def test_extract_search_keywords_finds_model_ids():
    r = _router()
    kws = r.extract_search_keywords("What is the RK3588 UART count?")
    assert "RK3588" in kws


def test_extract_search_keywords_plain_words_empty():
    r = _router()
    assert r.extract_search_keywords("what is this document about") == []


# ---- HierarchicalRetriever._tokenize_query ----
def test_tokenize_query_cjk_and_ascii():
    r = _retriever()
    kw = r._tokenize_query("RK3588 的详细规格是什么")
    assert "RK3588" in kw
    assert any("详细规格" in k for k in kw)
    assert any("是什么" in k for k in kw)


def test_tokenize_query_excludes_pure_digits():
    r = _retriever()
    assert r._tokenize_query("version 2024 compare") == ["version", "compare"]
    assert r._tokenize_query("12345") == []


# ---- SegmentMerger._smart_truncate ----
def test_smart_truncate_short_text_returned_unchanged():
    m = SegmentMerger()
    text = "Short content."
    assert m._smart_truncate(text, "question", 100) == text


def test_smart_truncate_keeps_keyword_sentences_within_cap():
    m = SegmentMerger()
    filler = "这是一段与问题无关的填充内容。" * 20
    text = "目标芯片型号为 RK3588，支持 4 路 UART。" + filler
    out = m._smart_truncate(text, "RK3588 UART", 60)
    # content is capped at max_len; a small fixed prefix (excerpt marker) is
    # prepended by the merger, so allow headroom for it
    assert len(out) <= 100
    assert "RK3588" in out


# ---- SegmentMerger._get_content_cap ----
def test_content_cap_tiers():
    m = SegmentMerger()
    cfg = {"merger_content_cap_top": 16000, "merger_content_cap_high": 10000,
           "merger_content_cap_medium": 6000, "merger_content_cap_low": 3000,
           "merger_content_cap_floor": 3000}
    assert m._get_content_cap(6.0, cfg) == 16000
    assert m._get_content_cap(2.5, cfg) == 10000
    assert m._get_content_cap(0.6, cfg) == 6000
    assert m._get_content_cap(0.2, cfg) == 3000
    assert m._get_content_cap(0.0, cfg) == 3000


# ---- SegmentMerger._match_boost_rule ----
def test_match_boost_rule():
    m = SegmentMerger()
    rules = {
        "spec_query": {
            "query_keywords": ["uart"],
            "boost_sections": [{"keywords": ["uart"], "boost": 1.5}],
            "penalize_sections": [{"keywords": ["history"], "penalty": -1.0}],
        }
    }
    assert m._match_boost_rule("uart count", "UART Interfaces", rules) == 1.5
    assert m._match_boost_rule("uart count", "History", rules) == -1.0
    assert m._match_boost_rule("other query", "UART Interfaces", rules) == 0.0
    assert m._match_boost_rule("uart count", "UART Interfaces", {}) == 0.0


# ---- QueryPlanner entity stopwords (core meta-words + pack-injected) ----
def test_entity_stopwords_filter_generic_terms():
    """Generic Chinese query words must not be treated as document entities
    (cross-doc contamination variant: '公司' force-merges unrelated
    '...股份有限公司...' reports into the doc_filter). Domain words like
    '公司'/'营业收入' live in the generic pack's entity_stopwords and reach
    the planner via RetrievalPlugin.get_entity_stopwords(); core meta-words
    ('多少'...) are always active."""
    import re

    import core.plugins as plugins_mod
    from core.retrieval.planner import QueryPlanner

    text = "在美的集团(股票代码000333)的2025年年度报告中，公司2025年度营业收入是多少？"
    cn = re.findall(r'[\u4e00-\u9fff]{2,12}', text)

    # Core-only: meta-words filtered, domain words NOT (they are the pack's
    # job — a bare core must not know annual-report vocabulary).
    core_kept = [w for w in cn if w not in QueryPlanner._CN_ENTITY_STOPWORDS]
    assert "多少" not in core_kept
    assert "公司" in core_kept  # not core's business anymore

    # With the generic pack loaded the full filter is restored.
    old_registry = plugins_mod._registry
    QueryPlanner._STOPWORDS_CACHE = None
    try:
        plugins_mod._registry = plugins_mod.PluginRegistry(
            scan_dirs=[str(Path(__file__).resolve().parent.parent
                           / "industries")])
        QueryPlanner._STOPWORDS_CACHE = None
        stopwords = QueryPlanner._all_entity_stopwords()
        kept = [w for w in cn if w not in stopwords]
        assert "公司" not in kept
        assert "股票代码" not in kept
        assert "营业收入" not in kept
        # The real entity must survive
        assert any("美的集团" in w for w in kept)
    finally:
        plugins_mod._registry = old_registry
        QueryPlanner._STOPWORDS_CACHE = None


def test_entity_stopwords_keeps_real_entity():
    from core.retrieval.planner import QueryPlanner
    assert "美的集团" not in QueryPlanner._CN_ENTITY_STOPWORDS
    assert "贵州茅台" not in QueryPlanner._CN_ENTITY_STOPWORDS
    # Domain vocabulary must NOT live in core (pack-injected instead).
    assert "公司" not in QueryPlanner._CN_ENTITY_STOPWORDS
    assert "营业收入" not in QueryPlanner._CN_ENTITY_STOPWORDS
    # Domain-neutral meta-words stay in core.
    assert "多少" in QueryPlanner._CN_ENTITY_STOPWORDS
    assert "是什么" in QueryPlanner._CN_ENTITY_STOPWORDS


# ---- HierarchicalRetriever._expand_query_terms ----
def test_expand_query_terms_no_pack_returns_original():
    """Without an industry pack the expansion must be a no-op: core generic
    terms (数量/版本/...) stay exclusive to spec-fact lookup and never touch
    FTS keywords (pure function path, no registry -> no plugin detected)."""
    r = _retriever()
    kw = r._expand_query_terms(["版本"], "版本是多少")
    assert kw == ["版本"]  # unchanged: zero impact without packs


def test_expand_query_terms_returns_original_when_empty():
    r = _retriever()
    kw = r._expand_query_terms([], "no terms")
    assert kw == []


def test_expand_query_terms_asks_the_retrieval_side_of_the_pack(monkeypatch):
    """A pack's vocabulary lives on its retrieval plugin, not on the composite
    plugin the registry hands out (true of every shipped pack: the composite does
    not expose ``get_spec_query_terms``). Asking the composite made the hasattr()
    guard false, which disabled the expansion for every pack, silently."""
    pack = SimpleNamespace(retrieval=SimpleNamespace(
        get_spec_query_terms=lambda: {"gpu": ["graphics engine"]}))
    registry = SimpleNamespace(detect_plugin_for_text=lambda text: pack)
    monkeypatch.setattr("core.plugins.get_plugin_registry", lambda: registry)

    assert _retriever()._expand_query_terms(["GPU"], "GPU 型号") == ["GPU", "graphics engine"]


def test_expand_query_terms_matches_declared_terms_case_insensitively():
    """Packs declare the term in lowercase ("gpu"); queries are written "GPU".
    The pack's own comment states the match is case-insensitive."""
    r = _retriever()

    lower = [t.lower() for t in r._expand_query_terms(["RK3562", "gpu"], "RK3562 的 gpu 型号是什么")]
    upper = [t.lower() for t in r._expand_query_terms(["RK3562", "GPU"], "RK3562 的 GPU 型号是什么")]

    assert "graphics engine" in lower
    assert "graphics engine" in upper, (
        "the declared term is lowercase, so an uppercase query must reach it too"
    )


def test_expand_query_terms_keeps_the_original_keywords_first():
    """Expansion appends; it must never reorder or replace the query's own terms,
    because those carry the exact matches FTS is relied on for."""
    expanded = _retriever()._expand_query_terms(["RK3562", "GPU"], "RK3562 的 GPU 型号是什么")

    assert expanded[:2] == ["RK3562", "GPU"]
    assert "graphics engine" in [t.lower() for t in expanded], (
        "the chapter title that states the value must become reachable"
    )


def test_expand_query_terms_can_be_switched_off(monkeypatch):
    """The rollback switch restores the pack-independent keyword set exactly, so
    the expansion can be A/B'd and reverted without a rebuild (used by the
    2026-09-20 attribution run)."""
    monkeypatch.setitem(settings.CONTEXT_CONFIG, "pack_term_expansion", False)

    kw = _retriever()._expand_query_terms(["RK3562", "GPU"], "RK3562 的 GPU 型号是什么")

    assert kw == ["RK3562", "GPU"], "off means the caller's keywords, untouched"


def test_expand_query_terms_is_on_by_default():
    """A missing key must not silently disable the expansion."""
    assert settings.CONTEXT_CONFIG.get("pack_term_expansion", True) is True
