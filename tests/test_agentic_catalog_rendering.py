"""The agentic document catalog must reach the model whole.

A raw character slice of the catalog JSON used to cut the listing mid-document,
so on a moderately large collection the tail never reached the model and a
query naming those documents produced no target documents at all — the agent
then fell back to a tenant-wide sweep and answered from unrelated documents.
"""
import json

from core.retrieval.agentic_retriever import AgenticRetriever


def _agent(docs):
    agent = AgenticRetriever.__new__(AgenticRetriever)
    agent.catalog = {"documents": docs}
    return agent


def _docs(n, chapters=6):
    return [{"doc_id": f"doc{i:03d}", "title": f"Report {i}",
             "chapters": [{"title": f"Chapter {j}", "pages": f"{j}-{j}"}
                          for j in range(chapters)]}
            for i in range(n)]


def test_small_catalog_keeps_chapter_detail():
    agent = _agent(_docs(2))
    out = agent._render_catalog(10_000)
    parsed = json.loads(out.strip())
    assert len(parsed["documents"]) == 2
    assert parsed["documents"][0]["chapters"]


def test_large_catalog_keeps_every_document():
    docs = _docs(40)
    agent = _agent(docs)
    out = agent._render_catalog(4000)
    assert len(out) <= 4200  # budget plus the short trailer
    for doc in docs:
        assert doc["doc_id"] in out, f"{doc['doc_id']} missing from the listing"


def test_truncation_says_so_instead_of_silently_shortening():
    agent = _agent(_docs(400, chapters=1))
    out = agent._render_catalog(1000)
    assert "more documents are not listed here" in out
    # Whatever survives must still be parseable, not a broken JSON fragment.
    body = out.split("\n")[0]
    json.loads(body)


def test_empty_catalog_renders_valid_json():
    out = _agent([])._render_catalog(4000)
    assert json.loads(out.strip()) == {"documents": []}
