"""What retrieval actually did must be recorded — and recorded per request.

The audit row (``query_log.trace_json``) held only ``session_id`` and
``auto_created``, so a retrieval outcome could not be explained after the fact:
"the value is in the database but the answer says it is not" stalled at
guesswork. These tests pin the properties that make the record usable:

* the collector is bounded, and a truncated list is never mistaken for the whole
  result set (``<field>_count`` carries the true length);
* an empty collector reports ``None``, which is what lets the engine tell
  "answered without an agentic trace" apart from "nothing to say";
* the trace travels retriever -> engine -> audit row unchanged, and it belongs to
  one request only: ``QueryEngine`` caches components per tenant (see
  ``tests/test_executor_request_isolation.py``), so per-request state must never
  ride on anything shared.
"""

from types import SimpleNamespace

import pytest

from core.api.routes import query as query_routes
from core.retrieval.agentic_retriever import AgenticRetriever
from core.retrieval.engine import QueryEngine
from core.retrieval.retrieval_trace import MAX_LIST_ITEMS, MAX_TEXT_CHARS, RetrievalTrace

# ── the collector ────────────────────────────────────────────────────────────

def test_an_empty_trace_is_none_so_absence_stays_visible():
    assert RetrievalTrace().to_dict() is None


def test_a_truncated_list_keeps_its_true_length():
    trace = RetrievalTrace()
    pages = list(range(MAX_LIST_ITEMS * 3))

    trace.record("fts", hits=pages)

    entry = trace.to_dict()["fts"]
    assert entry["hits"] == pages[:MAX_LIST_ITEMS]
    assert entry["hits_count"] == len(pages), (
        "a truncated list must never read as the whole result set"
    )


def test_long_strings_are_capped():
    trace = RetrievalTrace()

    trace.record("fts", query="x" * (MAX_TEXT_CHARS * 4))

    stored = trace.to_dict()["fts"]["query"]
    assert stored.startswith("x" * MAX_TEXT_CHARS)
    assert len(stored) < MAX_TEXT_CHARS * 2


def test_sections_accumulate_instead_of_replacing_each_other():
    trace = RetrievalTrace()

    trace.record("keywords", doc="AB1234", expanded=["Graphics Engine"])
    trace.record("keywords", doc="AB5678", expanded=["Microprocessor"])
    trace.record("merge", branch="fts_first")

    sections = trace.to_dict()
    assert sections["keywords"]["doc"] == "AB5678", "a later record must update its own fields"
    assert sections["merge"]["branch"] == "fts_first", "sections must not overwrite one another"


def test_scalars_keep_their_type():
    trace = RetrievalTrace()

    trace.record("merge", has_exact=True, fts_count=3)

    entry = trace.to_dict()["merge"]
    assert entry["has_exact"] is True, "a boolean flag must not be stringified"
    assert entry["fts_count"] == 3


def test_record_is_chainable_and_note_explains_a_section():
    trace = RetrievalTrace().record("vector", hits=[]).note("vector", "no vectors for this tenant")

    assert trace.to_dict()["vector"]["note"] == "no vectors for this tenant"


# ── the retriever ────────────────────────────────────────────────────────────

def _retriever():
    """A retriever with ``__init__`` bypassed, the way the contract tests build it.

    ``__init__`` loads the tenant's whole vector index and metadata catalog;
    diagnostics must never be the reason retrieval fails.
    """
    agent = AgenticRetriever.__new__(AgenticRetriever)
    agent.tenant_id = "admin"
    return agent


def test_a_retriever_built_without_init_still_traces():
    agent = _retriever()

    assert agent.trace.to_dict() is None
    agent.trace.record("keywords", expanded=["Graphics Engine"])
    assert agent.trace.to_dict()["keywords"]["expanded"] == ["Graphics Engine"]


def test_two_requests_do_not_share_a_trace():
    first, second = _retriever(), _retriever()

    first.trace.record("fts", hits=[{"page": 9}] )

    assert second.trace.to_dict() is None, "diagnostics must be request-scoped"


def _fts(page_id, score):
    return {"page_id": page_id, "page_num": page_id, "score": score, "source": "fts"}


def test_a_decisive_fts_hit_is_recorded_and_the_vector_channel_is_skipped(monkeypatch):
    agent = _retriever()
    monkeypatch.setattr(agent, "_fts_search", lambda *a, **k: [_fts(9, 9.0), _fts(11, 8.5)])
    monkeypatch.setattr(agent, "_semantic_search",
                        lambda *a, **k: pytest.fail("vector search ran despite a decisive FTS hit"))

    results = agent._hybrid_search("q")

    merge = agent.trace.to_dict()["merge"]
    assert merge["branch"] == "fts_first", "which branch ran is the whole point of the record"
    assert [hit["page"] for hit in merge["topk"]] == [9, 11]
    assert len(results) == 2


def test_a_weak_fts_hit_is_recorded_as_a_merge(monkeypatch):
    agent = _retriever()
    monkeypatch.setattr(agent, "_fts_search", lambda *a, **k: [_fts(9, 1.0)])
    monkeypatch.setattr(agent, "_semantic_search",
                        lambda *a, **k: [(0.42, {"page_id": 7, "chunk_idx": 1, "page_num": 7,
                                                 "chunk_text": "text"})])

    agent._hybrid_search("q")

    merge = agent.trace.to_dict()["merge"]
    assert merge["branch"] == "hybrid_merge"
    assert merge["fts_count"] == 1
    assert merge["vector_count"] == 1


def test_what_the_search_steps_recorded_reaches_the_caller(monkeypatch):
    doc = {"doc_id": "d1", "title": "AB1234 Datasheet", "reason": "test", "target_pages": []}
    agent = _retriever()
    agent.spec_facts_plan = {}
    agent.config = {}
    agent.catalog = {"documents": [doc]}
    agent.vec_index = []
    agent.model_client = SimpleNamespace(
        generate_json=lambda prompt, **kwargs: {"target_docs": [doc]},
        generate=lambda prompt, **kwargs: "answer",
    )

    def query_single(query, doc_id, title):
        agent.trace.record("fts", scoped_to_doc=True, hits=[{"page": 3, "score": 9.0}])
        return {"has_answer": True, "answer": "3.3 V", "sources": [{"doc_id": doc_id, "pages": [3]}]}

    monkeypatch.setattr(agent, "_query_single_document", query_single)

    result = agent.retrieve("what voltage does AB1234 need?")

    assert result["retrieval_trace"]["fts"]["hits"] == [{"page": 3, "score": 9.0}]


# ── the engine ───────────────────────────────────────────────────────────────

def _retrieval_result(trace="absent"):
    result = {"context": "x" * 80, "sources": [{"doc_id": "d1", "pages": [1]}],
              "total_results": 1, "total_chars": 80}
    if trace != "absent":
        result["retrieval_trace"] = trace
    return result


def _engine(monkeypatch, retrieval_result):
    engine = QueryEngine()
    planner = SimpleNamespace(plan=lambda q, h: {"rewritten_query": q, "entities": [],
                                                 "routed_category": "", "intent": "lookup"})
    monkeypatch.setattr(engine, "_get_components", lambda tenant_id: {
        "planner": planner,
        "executor": SimpleNamespace(execute=lambda plan, **kw: retrieval_result),
        "synthesizer": SimpleNamespace(synthesize=lambda **kw: {"answer": "A", "sources": []}),
        "metadata_db": None,
    })
    monkeypatch.setattr(engine, "_classify_query", lambda q: "traditional")
    monkeypatch.setattr(engine, "_lookup_spec_facts", lambda *a, **k: [])
    monkeypatch.setattr(engine, "router",
                        SimpleNamespace(route=lambda q: SimpleNamespace(
                            intent=SimpleNamespace(value="lookup"))))
    return engine


def test_the_engine_passes_the_trace_through_unchanged(monkeypatch):
    trace = {"merge": {"branch": "hybrid_merge", "fts_count": 0}}
    engine = _engine(monkeypatch, _retrieval_result(trace))

    result = engine.query("q", tenant_id="admin")

    assert result["retrieval_trace"] == trace


def test_the_engine_says_so_when_the_decomposed_path_answered(monkeypatch):
    engine = _engine(monkeypatch, _retrieval_result())

    result = engine.query("q", tenant_id="admin")

    note = result["retrieval_trace"]["agentic"]["note"]
    assert note, "an absent agentic trace must be explained, not left as a bare None"


def test_the_empty_retrieval_guard_still_reports_the_trace(monkeypatch):
    trace = {"fts": {"hits": [], "hits_count": 0}}
    engine = _engine(monkeypatch, {"context": "", "sources": [], "total_results": 0,
                                   "total_chars": 0, "retrieval_trace": trace})

    result = engine.query("q", tenant_id="admin")

    assert result["retrieval_trace"] == trace


# ── the audit row ────────────────────────────────────────────────────────────

def _audit_row(monkeypatch):
    """Run ``_persist_query_result`` against a stand-in tenant DB, capture log_query."""
    captured = {}

    class _DB:
        def create_chat_session(self, *a, **k):
            pass

        def save_chat_message(self, *a, **k):
            pass

        def log_query(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("core.db.tenant_db.get_tenant_metadata_db", lambda tenant_id: _DB())
    ctx = SimpleNamespace(tenant_id="admin", user_id="u1")
    req = SimpleNamespace(query="q", session_id="s1", industry="auto")
    return ctx, req, captured


def test_the_audit_row_records_what_retrieval_did(monkeypatch):
    ctx, req, captured = _audit_row(monkeypatch)
    trace = {"merge": {"branch": "fts_first"}}

    query_routes._persist_query_result(
        ctx, req, {"answer": "a", "sources": [], "retrieval_trace": trace}, 12)

    assert captured["trace"]["retrieval"] == trace


def test_an_answer_without_retrieval_diagnostics_records_that_explicitly(monkeypatch):
    ctx, req, captured = _audit_row(monkeypatch)

    query_routes._persist_query_result(ctx, req, {"answer": "a", "sources": []}, 12)

    assert "retrieval" in captured["trace"]
    assert captured["trace"]["retrieval"] is None, (
        "a missing field would be indistinguishable from a change in the audit format"
    )
