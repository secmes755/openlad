"""Contract tests for AgenticRetriever — previously no coverage at all.

``__init__`` is bypassed (it loads the tenant's whole vector index and metadata
catalog); the pieces that decide behaviour are exercised directly:
``_load_vec_index`` against a real SQLite file, the document-selection rules, the
per-model search loop and its early exit, and the two result shapes.
"""
import sqlite3
import struct
from types import SimpleNamespace

import pytest

from core.retrieval.agentic_retriever import AgenticRetriever


def _agent(catalog, json_reply=None, generate_reply="synthesised", prompts=None):
    agent = AgenticRetriever.__new__(AgenticRetriever)
    agent.tenant_id = "admin"
    agent.spec_facts_plan = {}
    agent.config = {}
    agent.catalog = catalog
    agent.vec_index = []

    def generate_json(prompt, **kwargs):
        if prompts is not None:
            prompts.append(prompt)
        return json_reply if json_reply is not None else {"target_docs": []}

    agent.model_client = SimpleNamespace(
        generate_json=generate_json,
        generate=lambda prompt, **kwargs: generate_reply,
    )
    return agent


def _doc(doc_id, title):
    return {"doc_id": doc_id, "title": title, "reason": "test", "target_pages": []}


# ── the index load ───────────────────────────────────────────────────────────

def test_load_vec_index_unpacks_embeddings(monkeypatch, tmp_path):
    vec_path = tmp_path / "vec.db"
    conn = sqlite3.connect(vec_path)
    conn.execute("CREATE TABLE l2_chunks (page_id INTEGER, chunk_idx INTEGER, doc_id TEXT, "
                 "embedding BLOB, chunk_text_preview TEXT, chunk_text TEXT)")
    conn.execute("INSERT INTO l2_chunks VALUES (?,?,?,?,?,?)",
                 (7, 0, "d1", struct.pack("3f", 0.5, 1.5, -2.0), "preview", "full text"))
    conn.commit()
    conn.close()

    agent = _agent({"documents": []})
    monkeypatch.setattr("core.retrieval.agentic_retriever.settings.get_tenant_vec_db_path",
                        lambda tid: str(vec_path))

    chunks = agent._load_vec_index()

    assert len(chunks) == 1
    assert chunks[0]["page_id"] == 7
    assert chunks[0]["doc_id"] == "d1"
    assert chunks[0]["chunk_idx"] == 0
    assert chunks[0]["preview"] == "preview"
    assert chunks[0]["chunk_text"] == "full text"
    assert list(chunks[0]["embedding"]) == pytest.approx([0.5, 1.5, -2.0])


# ── document selection ───────────────────────────────────────────────────────

def test_an_empty_catalog_yields_no_results(monkeypatch):
    agent = _agent({"documents": []})
    monkeypatch.setattr(agent, "_query_single_document",
                        lambda *a, **k: pytest.fail("nothing to search cannot be searched"))

    result = agent.retrieve("compare AB1234 and AB5678")

    assert result["strategy"] == "agentic_retrieve"
    assert result["total_results"] == 0
    assert result["context"] == ""
    assert result["sources"] == []
    assert result["query"] == "compare AB1234 and AB5678"


def test_a_model_named_in_the_query_is_forced_into_the_target_set(monkeypatch):
    """The model may select nothing; a model named in the query is still searched."""
    catalog = {"documents": [{"doc_id": "d1", "title": "AB1234 Datasheet", "chapters": []}]}
    prompts = []
    agent = _agent(catalog, json_reply={"target_docs": []}, prompts=prompts)

    searched = []
    monkeypatch.setattr(agent, "_query_single_document",
                        lambda query, doc_id, title: searched.append(doc_id) or {"has_answer": False})

    result = agent.retrieve("what is the power consumption of AB1234?")

    assert "AB1234 Datasheet" in prompts[0], "the model must see the catalog to choose from"
    assert searched == ["d1"], "the force-include rule must override an empty selection"
    assert result["total_results"] == 0


def test_the_search_stops_at_the_first_document_that_answers(monkeypatch):
    """One product model is answered by one document; the rest of its group is skipped."""
    docs = [_doc("d1", "AB1234 Datasheet"), _doc("d2", "AB1234 Manual"), _doc("d3", "AB1234 Guide")]
    agent = _agent({"documents": docs}, json_reply={"target_docs": docs})

    answers = {"d1": False, "d2": True, "d3": True}
    searched = []

    def query_single(query, doc_id, title):
        searched.append(doc_id)
        return {"has_answer": answers[doc_id], "answer": f"answer from {doc_id}", "sources": []}

    monkeypatch.setattr(agent, "_query_single_document", query_single)

    agent.retrieve("compare the interfaces")

    assert searched == ["d1", "d2"], "the group must stop once a document answers"


# ── result shapes ────────────────────────────────────────────────────────────

def test_the_success_path_returns_the_documented_shape(monkeypatch):
    doc = _doc("d1", "AB1234 Datasheet")
    agent = _agent({"documents": [doc]}, json_reply={"target_docs": [doc]},
                   generate_reply="AB1234 needs 3.3 V.")
    monkeypatch.setattr(agent, "_query_single_document",
                        lambda query, doc_id, title: {
                            "has_answer": True,
                            "answer": "3.3 V",
                            "sources": [{"doc_id": doc_id, "pages": [1, 2]}],
                        })

    result = agent.retrieve("what voltage does AB1234 need?")

    assert set(result) >= {"query", "context", "sources", "total_results", "total_chars",
                           "strategy", "answer"}
    assert result["strategy"] == "agentic_retrieve"
    assert result["answer"] == "AB1234 needs 3.3 V."
    assert result["total_results"] == 2, "counted from the sources' pages"
    assert result["total_chars"] == len(result["context"])
    assert "AB1234" in result["context"], "the per-document answers reach the context"
