"""Tenant, evidence, and response contracts for document tools."""
import json

import pytest

from core.agent_runtime import (
    AgentBudget,
    AgentContext,
    AgentRuntime,
    EvidenceStore,
    document_tools,
)
from core.agent_runtime.document_tools import (
    create_document_registry,
    lookup_facts,
    read_pages,
    search_keyword,
    search_structure,
    search_vector,
)
from core.agent_runtime.evidence import (
    citation_coverage,
    extract_citations,
    extract_page_references,
    has_absolute_claim,
    readable_evidence_payload,
)
from core.db.tenant_db import TenantMetadataDB
from core.models.client import ChatResult

A = "a" * 64
B = "b" * 64


def test_parenthesized_page_ranges_require_every_page_to_be_read():
    store = EvidenceStore()
    store.mark_read({"doc_id": A, "page_num": 1, "content": "one"})
    store.mark_read({"doc_id": A, "page_num": 2, "content": "two"})

    audit = store.audit_answer(f"Facts (d:{A}, p:1-2)")
    assert audit["verifiable"] == [(A, 1), (A, 2)]
    assert audit["unverifiable"] == []

    unread = store.audit_answer(f"Facts (d:{A}, p:1-3)")
    assert unread["unverifiable"] == [(A, 3)]
    assert extract_citations(f"Invalid (d:{A}, p:1-1000)") == []


def test_page_only_citations_resolve_only_against_one_readable_document():
    store = EvidenceStore()
    for page in (9, 10, 11):
        store.mark_read({"doc_id": A, "page_num": page, "content": f"page {page}"})

    audit = store.audit_answer("Alpha p.9; Beta pp.10–11; repeated p.9")
    assert audit["verifiable"] == [(A, 9), (A, 10), (A, 11)]
    assert audit["unverifiable"] == []
    assert audit["ambiguous_page_references"] == []
    assert store.audit_answer("Unread p.12")["unverifiable"] == [(A, 12)]
    assert extract_page_references(f"Explicit (d:{A}, p:9-10), p.11") == [11]
    assert extract_page_references("Invalid p.1-1000") == []

    store.mark_read({"doc_id": B, "page_num": 9, "content": "other document"})
    ambiguous = store.audit_answer("Ambiguous p.9")
    assert ambiguous["ambiguous_page_references"] == [9]
    assert ambiguous["cited"] == []


def test_absolute_claim_guard_ignores_explicit_uncertainty_not_real_negations():
    assert has_absolute_claim("Alpha 不支持 Beta。")
    assert has_absolute_claim("其他信息无法确认。\nAlpha 不支持 Beta。")
    assert not has_absolute_claim("无法确认是“不支持”还是“文档未列出”。")
    assert not has_absolute_claim("未读取页面不作为“不支持”的结论。")
    assert not has_absolute_claim("未找到页面并不等于不存在相关资料。")


def test_evidence_payload_preserves_full_pages_and_exposes_truncation():
    store = EvidenceStore()
    body = ("a" * 1000) + "TARGET" + ("b" * 1500)
    store.mark_read({"doc_id": A, "page_num": 9, "offset": 0, "content": body})
    store.mark_read({"doc_id": A, "page_num": 9, "offset": len(body), "content": "TAIL"})
    store.mark_read({"doc_id": B, "page_num": 12, "content": "short"})

    full = readable_evidence_payload(store, 10000)
    assert "TARGET" in full[0]["content"] and "TAIL" in full[0]["content"]
    assert all(not item["truncated"] for item in full)
    assert [item["included_chars"] for item in full] == [len(body) + 4, 5]

    limited = readable_evidence_payload(store, 800)
    assert len(limited) == 2
    assert limited[0]["content_chars"] == len(body) + 4
    assert limited[0]["truncated"] is True
    assert limited[0]["included_chars"] < limited[0]["content_chars"]
    assert limited[1]["truncated"] is False
    assert sum(item["included_chars"] for item in limited) <= 800


def test_statement_citation_coverage_is_diagnostic_not_semantic_proof():
    coverage = citation_coverage(
        f"Alpha is 5 units (d:{A}, p:1).\nBeta is 6 units.")

    assert coverage["statements"] == 2
    assert coverage["cited_statements"] == 1
    assert coverage["coverage"] == 0.5
    assert coverage["semantic_support_verified"] is False
    assert citation_coverage("Alpha is 5 units, p.9.")["cited_statements"] == 1


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


def test_scoped_insufficient_response_does_not_require_irrelevant_citation(env):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    model = NativeModel([
        tool_call("search_keyword", {"query": "Alpha"}),
        ChatResult("未找到相关文档，无法回答。", "stop"),
    ])
    result = AgentRuntime(create_document_registry(), model).run(
        "Missing", tenant_id="tenant-a")

    assert result["status"] == "completed"
    assert result["evidence"]["candidates"] > 0
    assert result["answer_disposition"] == "insufficient_evidence"


def test_absolute_claim_does_not_get_insufficient_evidence_exemption(env):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    context = AgentContext(tenant_id="tenant-a", question="Alpha")
    search_keyword(context, {"query": "Alpha"})

    error = AgentRuntime._response_policy_error(context, "Alpha is unsupported.")

    assert error == "answer has no verifiable citation to readable evidence"

    mixed_response = "Alpha is 5 units.\n" + "x" * 350 + "\n文档未明确提及。"
    assert AgentRuntime._response_policy_error(context, mixed_response) == error


@pytest.mark.parametrize("allow,expected", [(False, "degraded"), (True, "completed")])
def test_absolute_claim_requires_an_explicit_trusted_policy(env, allow, expected):
    env("tenant-a", [(A, "a.txt", "Alpha fact", "ready")])
    answer = f"Alpha does not support Beta [d:{A}, p:1]"
    model = NativeModel([
        tool_call("read_pages", {"refs": [{"doc_id": A, "page_num": 1}]}),
        ChatResult(answer, "stop"), ChatResult(answer, "stop"),
    ])
    result = AgentRuntime(create_document_registry(), model).run(
        "Alpha support", tenant_id="tenant-a", capability_id="constrained",
        budget=AgentBudget(allow_unreviewed_absolute_claims=allow))

    assert result["status"] == expected
    if not allow:
        assert result["error"] == "absolute claims require claim-level evidence review"


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
