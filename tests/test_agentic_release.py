"""The per-request agentic index must actually be released.

``AgenticRetriever`` is constructed per query and loads the tenant's entire vector
index into memory, but ``release()`` only logged a line — the index stayed
referenced until the garbage collector got to it. Worse, the engine's failure path
never called it at all, so a deep-research query that failed part-way held that
memory with nothing to drop it.
"""
from core.retrieval.agentic_retriever import AgenticRetriever
from core.retrieval.engine import QueryEngine


def test_release_drops_the_loaded_index():
    agent = AgenticRetriever.__new__(AgenticRetriever)
    agent.vec_index = [{"embedding": [0.1, 0.2]}] * 10
    agent.catalog = {"documents": [{"doc_id": "d1"}]}

    agent.release()

    assert agent.vec_index == [], "release() must drop the loaded vector index"
    assert agent.catalog == {}, "release() must drop the catalog"


def test_engine_releases_the_agent_even_when_retrieval_fails(monkeypatch):
    released = []

    class _FailingAgent:
        def __init__(self, tenant_id, spec_facts_plan=None):
            pass

        def retrieve(self, query):
            raise RuntimeError("agentic blew up")

        def release(self):
            released.append("released")

    monkeypatch.setattr("core.retrieval.engine.AgenticRetriever", _FailingAgent)

    assert QueryEngine()._execute_agentic("compare AB1234 and AB5678", "admin") is None
    assert released == ["released"], "the failure path must release the index too"


def test_engine_releases_the_agent_on_success(monkeypatch):
    released = []

    class _Agent:
        def __init__(self, tenant_id, spec_facts_plan=None):
            pass

        def retrieve(self, query):
            return {"total_results": 1, "sources": []}

        def release(self):
            released.append("released")

    monkeypatch.setattr("core.retrieval.engine.AgenticRetriever", _Agent)

    assert QueryEngine()._execute_agentic("q", "admin") == {"total_results": 1, "sources": []}
    assert released == ["released"]
