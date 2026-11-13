"""A planner decomposition must not be discarded when string decomposition fails.

The string-level decomposer only recognises Latin model tokens, so a query that
names several organisations in another script produced one sub-query and the
engine fell back to a tenant-wide sweep. That answered from unrelated documents
while the planner had already produced a correct per-document decomposition.

The reused step is also normalised: the planner emits ``sub_queries`` objects
while the executor consumes ``query`` as a string or list of strings.
"""
from core.retrieval.engine import QueryEngine


def _plan(subs, filters=None):
    return {"strategy": "decomposed_retrieve", "steps": [{
        "tool": "decomposed_retrieve",
        "query": "original",
        "sub_queries": subs,
        "doc_filter": filters or [],
    }]}


def test_multi_sub_query_plan_is_reusable_and_normalised():
    plan = _plan([{"query": "a revenue"}, {"query": "b revenue"},
                  {"query": "c revenue"}])
    step = QueryEngine._planned_step(plan)["steps"][0]
    assert step["query"] == ["a revenue", "b revenue", "c revenue"]
    assert "sub_queries" not in step


def test_string_sub_queries_pass_through():
    step = QueryEngine._planned_step(_plan(["a", "b"]))["steps"][0]
    assert step["query"] == ["a", "b"]


def test_multi_doc_filter_plan_is_reusable():
    step = QueryEngine._planned_step(
        _plan([{"query": "q"}], filters=["d1", "d2"]))["steps"][0]
    # A single sub-query collapses to a plain string, never to a dict.
    assert step["query"] == "q"
    assert step["doc_filter"] == ["d1", "d2"]


def test_single_target_plan_is_not_reused():
    assert QueryEngine._planned_step(_plan([{"query": "only one"}])) is None
    assert QueryEngine._planned_step(
        _plan([{"query": "q"}], filters=["d1"])) is None


def test_missing_or_malformed_plan_is_ignored():
    assert QueryEngine._planned_step(None) is None
    assert QueryEngine._planned_step({}) is None
    assert QueryEngine._planned_step({"steps": []}) is None
    assert QueryEngine._planned_step({"steps": [None]}) is None
    # Sub-query objects without a query string must not become "[None]".
    assert QueryEngine._planned_step(_plan([{}, {}])) is None
