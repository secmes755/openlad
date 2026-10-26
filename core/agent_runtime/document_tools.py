"""Tenant-scoped document capabilities exposed as model-selectable tools."""
from __future__ import annotations

import re

from ..db.tenant_db import get_tenant_metadata_db, get_tenant_vector_db
from ..models.client import get_model_client
from .contracts import AgentContext
from .tools import ToolDefinition, ToolRegistry

_DOC_ID_RE = re.compile(r"[0-9a-fA-F]{4,64}")
_SPACE_RE = re.compile(r"\s+")


def _mdb(context: AgentContext):
    return get_tenant_metadata_db(context.tenant_id)


def _vdb(context: AgentContext):
    return get_tenant_vector_db(context.tenant_id)


def _excerpt(text: str, limit: int = 800) -> str:
    value = _SPACE_RE.sub(" ", text or "").strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _resolve_doc(context: AgentContext, mdb, value: str) -> str:
    value = str(value or "").strip().lower()
    if not _DOC_ID_RE.fullmatch(value):
        raise ValueError("doc_id must be a 4-64 character hexadecimal id/prefix")
    context.consume_db()
    with mdb.get_connection() as conn:
        if len(value) == 64:
            rows = conn.execute(
                "SELECT id FROM documents WHERE id=? AND status != 'deleted'",
                (value,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM documents WHERE id LIKE ? AND status != 'deleted' "
                "LIMIT 2", (value + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError("document id prefix is unknown or ambiguous")
    return rows[0]["id"]


def _doc_filter(context: AgentContext, mdb, values) -> set[str] | None:
    if not values:
        return None
    return {_resolve_doc(context, mdb, value) for value in values}


def _document(context: AgentContext, mdb, doc_id: str) -> dict:
    key = ("document", context.tenant_id, doc_id)
    if key not in context.cache:
        context.consume_db()
        context.cache[key] = mdb.get_document(doc_id) or {}
    return context.cache[key]


def _page_by_id(context: AgentContext, mdb, page_id) -> dict | None:
    key = ("page-id", context.tenant_id, page_id)
    if key not in context.cache:
        context.consume_db()
        context.cache[key] = mdb.get_page(page_id)
    return context.cache[key]


def _pages(context: AgentContext, mdb, doc_id: str) -> list[dict]:
    key = ("document-pages", context.tenant_id, doc_id)
    if key not in context.cache:
        context.consume_db()
        context.cache[key] = mdb.get_document_pages(doc_id)
    return context.cache[key]


def _active(context: AgentContext, mdb, doc_id: str) -> bool:
    doc = _document(context, mdb, doc_id)
    return bool(doc and doc.get("status") != "deleted")


def _page_record(context: AgentContext, mdb, page: dict, channel: str,
                 text: str) -> dict:
    doc = _document(context, mdb, page["doc_id"])
    return {
        "doc_id": page["doc_id"], "page_num": page["page_num"],
        "section_title": page.get("section_title") or "",
        "doc_name": doc.get("filename") or doc.get("title") or "",
        "channel": channel, "excerpt": _excerpt(text),
    }


def list_documents(context: AgentContext, args: dict) -> dict:
    mdb = _mdb(context)
    value = f"%{args.get('filter', '')}%"
    limit = args.get("limit", 20)
    context.consume_db()
    with mdb.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, filename, title, status FROM documents "
            "WHERE status != 'deleted' AND (title LIKE ? OR filename LIKE ?) "
            "ORDER BY filename LIMIT ?", (value, value, limit)).fetchall()
    items = [{"doc_id": row["id"], "filename": row["filename"],
              "title": row["title"]} for row in rows]
    return {"ok": True, "items": items, "health": [],
            "stats": {"returned": len(items)}}


def search_keyword(context: AgentContext, args: dict) -> dict:
    mdb = _mdb(context)
    doc_ids = _doc_filter(context, mdb, args.get("doc_ids"))
    limit = args.get("limit", 12)
    context.consume_db()
    chunks = mdb.search_fts_chunks(
        args["query"], limit=limit, doc_id_filter=doc_ids)
    items = []
    for chunk in chunks:
        if not _active(context, mdb, chunk["doc_id"]):
            continue
        page = _page_by_id(context, mdb, chunk.get("page_id"))
        if page:
            items.append(_page_record(
                context, mdb, page, "keyword",
                chunk.get("chunk_text") or page.get("raw_text") or ""))
    items = items[:limit]
    refs = context.evidence.add_candidates(items, "keyword")
    return {"ok": True, "items": items, "health": [],
            "stats": {"returned": len(items)}, "evidence_refs": refs}


def search_vector(context: AgentContext, args: dict) -> dict:
    mdb, vdb = _mdb(context), _vdb(context)
    doc_ids = _doc_filter(context, mdb, args.get("doc_ids"))
    limit = args.get("limit", 12)
    context.consume_embedding()
    embedding = get_model_client().embed(args["query"])
    context.consume_db()
    hits = vdb.search_l2_chunks(
        embedding, limit=limit * 2, doc_id_filter=doc_ids)
    items = []
    seen = set()
    for hit in hits:
        ref = (hit["doc_id"], hit.get("page_num"))
        if ref in seen or not _active(context, mdb, hit["doc_id"]):
            continue
        page = _page_by_id(context, mdb, hit.get("page_id"))
        if page:
            seen.add(ref)
            items.append(_page_record(
                context, mdb, page, "vector",
                hit.get("chunk_text") or page.get("raw_text") or ""))
        if len(items) >= limit:
            break
    refs = context.evidence.add_candidates(items, "vector")
    return {"ok": True, "items": items, "health": [],
            "stats": {"returned": len(items)}, "evidence_refs": refs}


def search_structure(context: AgentContext, args: dict) -> dict:
    mdb = _mdb(context)
    doc_ids = _doc_filter(context, mdb, args.get("doc_ids"))
    limit = args.get("limit", 12)
    like = f"%{args['query']}%"
    sql = (
        "SELECT s.doc_id, s.section_title, s.section_level, s.start_page, s.end_page, "
        "s.summary FROM doc_structure_index s JOIN documents d ON d.id=s.doc_id "
        "WHERE d.status != 'deleted' AND (s.section_title LIKE ? OR s.keywords LIKE ? "
        "OR s.entities LIKE ? OR s.summary LIKE ?)")
    params = [like, like, like, like]
    if doc_ids:
        sql += f" AND s.doc_id IN ({','.join('?' for _ in doc_ids)})"
        params.extend(sorted(doc_ids))
    sql += " ORDER BY s.start_page LIMIT ?"
    params.append(limit)
    context.consume_db()
    with mdb.get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [{
        "doc_id": row["doc_id"], "page_num": row["start_page"],
        "end_page": row["end_page"], "section_title": row["section_title"],
        "section_level": row["section_level"], "excerpt": row["summary"] or "",
    } for row in rows]
    refs = context.evidence.add_candidates(items, "structure")
    return {"ok": True, "items": items, "health": [],
            "stats": {"returned": len(items)}, "evidence_refs": refs}


def get_structure(context: AgentContext, args: dict) -> dict:
    mdb = _mdb(context)
    doc_id = _resolve_doc(context, mdb, args["doc_id"])
    depth = args.get("depth", 3)
    context.consume_db()
    with mdb.get_connection() as conn:
        rows = conn.execute(
            "SELECT section_title, section_level, start_page, end_page, summary "
            "FROM doc_structure_index WHERE doc_id=? AND section_level<=? "
            "ORDER BY start_page, section_level", (doc_id, depth)).fetchall()
    items = [dict(row) for row in rows]
    return {"ok": True, "items": items, "health": [],
            "stats": {"returned": len(items)}}


def lookup_facts(context: AgentContext, args: dict) -> dict:
    if not args.get("subject") and not args.get("attribute"):
        raise ValueError("subject or attribute is required")
    mdb = _mdb(context)
    doc_ids = _doc_filter(context, mdb, args.get("doc_ids"))
    clauses, params = [], []
    for column, key in (("entity", "subject"), ("attribute", "attribute")):
        if args.get(key):
            clauses.append(f"{column} LIKE ?")
            params.append(f"%{args[key]}%")
    sql = "SELECT doc_id, entity, attribute, value, unit, page_num, source_text FROM spec_facts"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if doc_ids:
        sql += (" WHERE " if not clauses else " AND ") + (
            f"doc_id IN ({','.join('?' for _ in doc_ids)})")
        params.extend(sorted(doc_ids))
    sql += " LIMIT ?"
    params.append(args.get("limit", 12))
    context.consume_db()
    with mdb.get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [{"doc_id": row["doc_id"], "page_num": row["page_num"],
              "subject": row["entity"], "attribute": row["attribute"],
              "value": row["value"], "unit": row["unit"],
              "excerpt": row["source_text"]} for row in rows]
    refs = context.evidence.add_candidates(items, "facts")
    return {"ok": True, "items": items, "health": [],
            "stats": {"returned": len(items)}, "evidence_refs": refs}


def read_pages(context: AgentContext, args: dict) -> dict:
    mdb = _mdb(context)
    items, health, refs = [], [], []
    for ref in args["refs"]:
        try:
            doc_id = _resolve_doc(context, mdb, ref["doc_id"])
            page_num = ref["page_num"]
            page = next((item for item in _pages(context, mdb, doc_id)
                         if item.get("page_num") == page_num), None)
            if not page:
                health.append({"kind": "not_found", "ref": ref})
                continue
            raw = page.get("raw_text") or ""
            offset = args.get("offset", 0)
            max_chars = args.get("max_chars", 4000)
            record = _page_record(context, mdb, page, "read", raw)
            record["offset"] = offset
            record["total_chars"] = len(raw)
            record["truncated"] = offset + max_chars < len(raw)
            record["content"] = raw[offset:offset + max_chars]
            items.append(record)
            refs.append(context.evidence.mark_read(record))
        except Exception as e:
            health.append({"kind": "read_error", "ref": ref, "message": str(e)[:300]})
    return {"ok": True, "items": items, "health": health,
            "stats": {"returned": len(items)},
            "evidence_refs": [ref for ref in refs if ref]}


def create_document_registry() -> ToolRegistry:
    query_properties = {
        "query": {"type": "string", "maxLength": 400},
        "doc_ids": {"type": "array", "maxItems": 4,
                    "items": {"type": "string", "maxLength": 64}},
        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
    }
    definitions = [
        ToolDefinition("list_documents", "List active documents in the current tenant.", {
            "type": "object", "properties": {
                "filter": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            }, "additionalProperties": False}, list_documents),
        ToolDefinition("search_keyword", "Search lexical document chunks; returns candidates, not readable evidence.", {
            "type": "object", "properties": query_properties,
            "required": ["query"], "additionalProperties": False}, search_keyword),
        ToolDefinition("search_vector", "Search semantic document chunks; returns candidates, not readable evidence.", {
            "type": "object", "properties": query_properties,
            "required": ["query"], "additionalProperties": False}, search_vector),
        ToolDefinition("search_structure", "Search document-provided section metadata and page ranges.", {
            "type": "object", "properties": query_properties,
            "required": ["query"], "additionalProperties": False}, search_structure),
        ToolDefinition("get_structure", "Get the section map for one document.", {
            "type": "object", "properties": {
                "doc_id": {"type": "string", "maxLength": 64},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5},
            }, "required": ["doc_id"], "additionalProperties": False}, get_structure),
        ToolDefinition("lookup_facts", "Look up structured facts by subject, attribute, or document.", {
            "type": "object", "properties": {
                "subject": {"type": "string", "maxLength": 200},
                "attribute": {"type": "string", "maxLength": 200},
                "doc_ids": query_properties["doc_ids"],
                "limit": query_properties["limit"],
            }, "additionalProperties": False}, lookup_facts),
        ToolDefinition("read_pages", "Read actual page content; only successful reads can support citations.", {
            "type": "object", "properties": {
                "refs": {"type": "array", "maxItems": 12, "items": {
                    "type": "object", "properties": {
                        "doc_id": {"type": "string", "maxLength": 64},
                        "page_num": {"type": "integer", "minimum": 1, "maximum": 1000000},
                    }, "required": ["doc_id", "page_num"],
                    "additionalProperties": False}},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 12000},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
            }, "required": ["refs"], "additionalProperties": False}, read_pages),
    ]
    return ToolRegistry(definitions)
