"""Tenant, evidence, and response contracts for document tools."""
import json

import pytest

from core.agent_runtime import AgentContext, AgentRuntime, document_tools
from core.agent_runtime.document_tools import (
    create_document_registry,
    lookup_facts,
    read_pages,
    search_keyword,
    search_structure,
    search_vector,
)
from core.db.tenant_db import TenantMetadataDB
from core.models.client import ChatResult

A = "a" * 64
B = "b" * 64


def make_db(path, docs):
    db = TenantMetadataDB(path)
    with db.get_connection() as conn:
        for doc_id, filename, text, status in docs:
            conn.execute(
                "INSERT INTO documents (id, filename, title, status) VALUES (?, ?, ?, ?)",
                (doc_id, filename, filename, status))
            page_id = conn.execute(
                "INSERT INTO doc_pages (doc_id, page_num, section_title, raw_text) "
                "VALUES (?, ?, ?, ?)", (doc_id, 1, "Overview", text)).lastrowid
            chunk_id = conn.execute(
                "INSERT INTO doc_chunks "
                "(doc_id, page_id, page_num, section_title, chunk_text, chunk_text_preview) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, page_id, 1, "Overview", text, text)).lastrowid
            conn.execute("INSERT INTO doc_chunks_fts(rowid, chunk_text) VALUES (?, ?)",
                         (chunk_id, text))
            conn.execute(
                "INSERT INTO doc_structure_index "
                "(doc_id, section_path, section_title, section_level, start_page, end_page, "
                "section_type, parent_path, keywords, summary, entities) "
                "VALUES (?, '1', 'Overview', 1, 1, 1, 'section', NULL, '', ?, '')",
                (doc_id, text))
            conn.execute(
                "INSERT INTO spec_facts "
                "(doc_id, entity, attribute, value, unit, page_num, source_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_id, "Alpha", "example", "5", "units", 1, text))
        conn.commit()
    return db


class FakeVector:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search_l2_chunks(self, _embedding, limit, doc_id_filter):
        self.calls.append((limit, doc_id_filter))
        return [hit for hit in self.hits
                if not doc_id_filter or hit["doc_id"] in doc_id_filter]


class FakeEmbed:
    def embed(self, _query):
        return [0.1, 0.2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbs, vectors = {}, {}
    monkeypatch.setattr(document_tools, "get_tenant_metadata_db", lambda tenant: dbs[tenant])
    monkeypatch.setattr(document_tools, "get_tenant_vector_db", lambda tenant: vectors[tenant])
    monkeypatch.setattr(document_tools, "get_model_client", lambda: FakeEmbed())

    def add(tenant, docs, hits=None):
        dbs[tenant] = make_db(tmp_path / f"{tenant}.db", docs)
        vectors[tenant] = FakeVector(hits or [])
        return dbs[tenant], vectors[tenant]

    return add


def tool_call(name, args, call_id="call-1"):
    return ChatResult("", "tool_calls", ({
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    },))


class NativeModel:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat_with_tools(self, *_args, **_kwargs):
        return self.replies.pop(0)


def test_document_tools_separate_candidates_from_readable_evidence(env):
    env("tenant-a", [(A, "alpha.txt", "Alpha fact", "ready")], [{
        "doc_id": A, "page_num": 1, "page_id": 1, "chunk_text": "Alpha fact",
    }])
    context = AgentContext(tenant_id="tenant-a", question="Alpha")

    keyword = search_keyword(context, {"query": "Alpha"})
    vector = search_vector(context, {"query": "Alpha"})
    structure = search_structure(context, {"query": "Overview"})
    facts = lookup_facts(context, {"subject": "Alpha"})

    assert keyword["items"] and vector["items"] and structure["items"] and facts["items"]
    assert context.evidence.snapshot()["candidates"] == 1
    assert context.evidence.snapshot()["readable_pages"] == 0

    read = read_pages(context, {"refs": [{"doc_id": "aaaaaaaa", "page_num": 1}]})
    assert read["items"][0]["content"] == "Alpha fact"
    audit = context.evidence.audit_answer("Alpha [d:aaaaaaaa, p:1]")
    assert audit["verifiable"] == [(A, 1)]
    assert context.evidence.sources(audit)[0]["doc_name"] == "alpha.txt"
    assert context.metrics()["embedding_calls"] == 1


def test_document_scoped_keyword_query_filters_inside_fts(env):
    env("tenant-a", [(A, "a.txt", "shared phrase", "ready"),
                     (B, "b.txt", "shared phrase", "ready")])
    context = AgentContext(tenant_id="tenant-a", question="shared")

    result = search_keyword(context, {"query": "shared", "doc_ids": ["bbbbbbbb"]})

    assert {item["doc_id"] for item in result["items"]} == {B}


def test_tenant_and_deleted_document_boundaries_are_enforced(env):
    env("tenant-a", [(A, "a.txt", "tenant secret", "ready")])
    env("tenant-b", [(B, "b.txt", "other content", "ready"),
                     ("c" * 64, "deleted.txt", "tenant secret", "deleted")])
    context = AgentContext(tenant_id="tenant-b", question="tenant secret")

    result = search_keyword(context, {"query": "tenant secret"})

    assert result["items"] == []
    assert context.evidence.snapshot()["candidates"] == 0


def test_missing_page_is_not_readable_evidence(env):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    context = AgentContext(tenant_id="tenant-a", question="Alpha")

    result = read_pages(context, {"refs": [{"doc_id": A, "page_num": 99}]})

    assert result["items"] == []
    assert result["health"][0]["kind"] == "not_found"
    assert context.evidence.snapshot()["readable_pages"] == 0


def test_read_pages_exposes_offsets_and_appends_segments(env):
    env("tenant-a", [(A, "a.txt", "0123456789", "ready")])
    context = AgentContext(tenant_id="tenant-a", question="numbers")

    first = read_pages(context, {
        "refs": [{"doc_id": A, "page_num": 1}], "offset": 4, "max_chars": 4})
    second = read_pages(context, {
        "refs": [{"doc_id": A, "page_num": 1}], "offset": 8, "max_chars": 4})

    assert first["items"][0]["content"] == "4567"
    assert first["items"][0]["total_chars"] == 10
    assert first["items"][0]["truncated"] is True
    assert second["items"][0]["content"] == "89"
    item = next(iter(context.evidence.items.values()))
    assert len(item.segments) == 2
    assert context.evidence.snapshot()["items"][0]["content_chars"] == 6


@pytest.mark.parametrize("doc_ref", ["aaaaaaaa", A])
def test_agent_cannot_cite_candidate_without_reading_it(env, doc_ref):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    model = NativeModel([
        tool_call("search_keyword", {"query": "Alpha"}),
        ChatResult(f"Alpha [d:{doc_ref}, p:1]", "stop"),
        ChatResult(f"Still unread [d:{doc_ref}, p:1]", "stop"),
    ])
    result = AgentRuntime(create_document_registry(), model).run(
        "Alpha", tenant_id="tenant-a")

    assert result["status"] == "degraded"
    assert result["finished"] is False
    assert result["answer"] == ""
    assert result["citation_audit"]["unverifiable"]


def test_agent_can_choose_search_read_and_verifiable_final_response(env):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    model = NativeModel([
        tool_call("search_keyword", {"query": "Alpha"}),
        tool_call("read_pages", {"refs": [{"doc_id": A, "page_num": 1}]}, "call-2"),
        ChatResult("Alpha fact [d:aaaaaaaa, p:1]", "stop"),
    ])
    result = AgentRuntime(create_document_registry(), model).run(
        "Alpha", tenant_id="tenant-a")

    assert result["status"] == "completed"
    assert result["sources"] == [{
        "doc_id": A, "page_num": 1, "doc_name": "a.txt", "section_title": "Overview"}]
    assert result["evidence"]["readable_pages"] == 1


def test_policy_feedback_lets_agent_read_and_correct_its_own_response(env):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    model = NativeModel([
        tool_call("search_keyword", {"query": "Alpha"}),
        ChatResult("Alpha [d:aaaaaaaa, p:1]", "stop"),
        tool_call("read_pages", {"refs": [{"doc_id": A, "page_num": 1}]}, "call-2"),
        ChatResult("Alpha fact [d:aaaaaaaa, p:1]", "stop"),
    ])
    result = AgentRuntime(create_document_registry(), model).run(
        "Alpha", tenant_id="tenant-a")

    assert result["status"] == "completed"
    assert result["sources"][0]["doc_id"] == A
    assert result["retrieval_trace"]["metrics"]["policy_feedback"] == 1
    assert [item["tool"] for item in result["retrieval_trace"]["observations"]] == [
        "search_keyword", "read_pages"]


def test_read_evidence_without_citation_degrades_instead_of_overclaiming(env):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    model = NativeModel([
        tool_call("search_keyword", {"query": "Alpha"}),
        tool_call("read_pages", {"refs": [{"doc_id": A, "page_num": 1}]}, "call-2"),
        ChatResult("Alpha fact", "stop"),
        ChatResult("Alpha fact without a source", "stop"),
    ])
    result = AgentRuntime(create_document_registry(), model).run(
        "Alpha", tenant_id="tenant-a")

    assert result["status"] == "degraded"
    assert result["answer"] == "Alpha fact without a source"
    assert result["evidence"]["readable_pages"] == 1
    assert "verifiable citation" in result["error"]
