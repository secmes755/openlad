"""Regression test: comparison/cross-reference synthesis branches must forward
original_query and explicit_pack_id to _synthesize_standard.

These branches used to drop the two arguments, silently disabling explicit
industry-pack selection and the table/language judgment fallbacks (which
deliberately use the original Chinese query) for comparison queries
(requires_comparison / VERSION_COMPARE / CROSS_REFERENCE).
"""
from core.retrieval.router import IntentType, QueryPlan
from core.retrieval.synthesizer import AnswerSynthesizer

CONTEXT = "x" * 200  # > 50 chars to pass the empty-context guard


def _make_plan(intent=IntentType.MACRO_QA, requires_comparison=False):
    return QueryPlan(
        intent=intent,
        raw_query="raw",
        entities=[],
        deep_explore=False,
        industry_hint=None,
        requires_comparison=requires_comparison,
    )


def _capture_branch(monkeypatch, plan):
    synth = object.__new__(AnswerSynthesizer)  # skip __init__ (heavy deps)
    captured = {}

    def fake_standard(query, plan, context, sources, chat_history=None,
                      routed_category=None, original_query=None,
                      explicit_pack_id=None):
        captured["original_query"] = original_query
        captured["explicit_pack_id"] = explicit_pack_id
        return {"answer": "ok", "sources": sources, "structured": False}

    monkeypatch.setattr(synth, "_synthesize_standard", fake_standard)
    synth.synthesize(
        query="rewritten query",
        plan=plan,
        context=CONTEXT,
        sources=[],
        chat_history=None,
        routed_category="semiconductor",
        original_query="中文原文查询",
        explicit_pack_id="financial",
    )
    return captured


def test_version_compare_branch_forwards_original_query_and_pack(monkeypatch):
    plan = _make_plan(intent=IntentType.VERSION_COMPARE)
    cap = _capture_branch(monkeypatch, plan)
    assert cap["original_query"] == "中文原文查询"
    assert cap["explicit_pack_id"] == "financial"


def test_cross_reference_branch_forwards_original_query_and_pack(monkeypatch):
    plan = _make_plan(intent=IntentType.CROSS_REFERENCE)
    cap = _capture_branch(monkeypatch, plan)
    assert cap["original_query"] == "中文原文查询"
    assert cap["explicit_pack_id"] == "financial"


def test_requires_comparison_branch_forwards_original_query_and_pack(monkeypatch):
    plan = _make_plan(requires_comparison=True)
    cap = _capture_branch(monkeypatch, plan)
    assert cap["original_query"] == "中文原文查询"
    assert cap["explicit_pack_id"] == "financial"
