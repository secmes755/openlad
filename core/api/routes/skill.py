"""
Agent Skill API Routes
Provides streamlined interfaces for AI Agents such as OpenClaw, HermesAgent, etc.

Shares query.py's global query lock; concurrency strategy is dynamically determined by hardware configuration
"""
import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...tenant.context import get_tenant_context
from .query import _query_lock  # shares global query lock

logger = logging.getLogger(__name__)

router = APIRouter()


class SkillQueryRequest(BaseModel):
    query: str
    industry: str | None = None
    return_format: str = "structured"  # structured | text
    max_results: int = 10


class SkillSearchRequest(BaseModel):
    query: str
    industry: str | None = None
    max_results: int = 10


class SkillIngestRequest(BaseModel):
    doc_id: str | None = None
    industry: str | None = None


@router.post("/query")
async def skill_query(req: SkillQueryRequest, request: Request):
    """Agent query endpoint (shares global concurrency lock)

    Returns structured JSON for easy parsing and use by Agents.
    Shares the global lock with /query; concurrency strategy is dynamically determined by hardware configuration.
    """
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")

    engine = getattr(request.app.state, "query_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Query engine not initialized. Service is starting up.")

    wait_start = time.time()
    async with _query_lock:
        wait_ms = int((time.time() - wait_start) * 1000)
        engine_start = time.time()
        result = await asyncio.to_thread(
            engine.query,
            query_text=req.query,
            tenant_id=ctx.tenant_id,
            industry_hint=req.industry
        )
        elapsed_ms = int((time.time() - engine_start) * 1000)
        result["wait_ms"] = wait_ms

    # Record query log (audit). Engine no longer logs — the route layer owns
    # the audit trail (mirrors routes/query.py); without this the Agent
    # channel's queries would have no query_log rows at all.
    try:
        from ...db.tenant_db import get_tenant_metadata_db
        db = get_tenant_metadata_db(ctx.tenant_id)
        db.log_query(
            query=req.query,
            user_id=ctx.user_id or "",
            intent=result.get("plan", {}).get("intent", "unknown"),
            industry_package_id=req.industry or "auto",
            elapsed_ms=elapsed_ms,
            results_count=len(result.get("sources", [])),
            answer_length=len(result.get("answer", "")),
            trace={"channel": "skill", "wait_ms": wait_ms}
        )
    except Exception as e:
        logger.warning(f"Failed to record query log: {e}")

    return {
        "query": req.query,
        "answer": result.get("answer", ""),
        "citations": result.get("sources", []),
        "confidence": result.get("confidence", "none"),
        "industry": req.industry or "auto",
        "tenant_id": ctx.tenant_id,
        "wait_ms": result.get("wait_ms", 0),
        "structured": {
            "summary": result.get("answer", "")[:200],
            "key_points": [],
            "references": result.get("sources", [])
        }
    }


@router.post("/search")
async def skill_search(req: SkillSearchRequest):
    """Agent search endpoint (shares global concurrency lock)

    Returns only a list of document fragments for the Agent to synthesize answers on its own.
    Shares the global lock with /query because the retrieval process may trigger embedding.
    """
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")

    from ...retrieval.retriever import HierarchicalRetriever
    from ...retrieval.router import IntentRouter

    wait_ms = 0
    try:
        wait_start = time.time()
        async with _query_lock:
            wait_ms = int((time.time() - wait_start) * 1000)

            router = IntentRouter()
            plan = router.route(req.query)

            retriever = HierarchicalRetriever(tenant_id=ctx.tenant_id)
            results = retriever.retrieve(
                query=req.query,
                plan=plan,
                max_results=req.max_results
            )

        fragments = []
        for r in results:
            fragments.append({
                "doc_id": r.doc_id,
                "page_id": r.page_id,
                "page_num": r.page_num,
                "score": round(r.score, 4),
                "content": r.content[:800] if r.content else "",
                "section_title": r.section_title or "",
                "filename": r.filename or "",
                "title": r.title or "",
                "text_source": r.text_source or "direct_extract",
            })

        return {
            "query": req.query,
            "fragments": fragments,
            "total": len(fragments),
            "tenant_id": ctx.tenant_id,
            "intent": plan.intent.value if plan else "unknown",
            "wait_ms": wait_ms
        }
    except Exception as e:
        logger.error(f"Skill search failed: {e}", exc_info=True)
        return {
            "query": req.query,
            "fragments": [],
            "total": 0,
            "tenant_id": ctx.tenant_id,
            "error": str(e)
        }


@router.get("/industries")
async def skill_list_industries():
    """Get available industry package list"""
    from ...plugins import get_plugin_registry
    registry = get_plugin_registry()
    return {
        "industries": [
            {"id": k, "name": v["name"], "description": v["description"]}
            for k, v in registry.list_plugins().items()
        ]
    }


@router.get("/status")
async def skill_status():
    """Get current tenant status"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")

    from ...tenant.tenant_manager import get_tenant_manager
    mgr = get_tenant_manager()
    tenant = mgr.get_tenant(ctx.tenant_id)
    usage = mgr.get_tenant_storage_usage(ctx.tenant_id)

    return {
        "tenant_id": ctx.tenant_id,
        "tenant_name": tenant.name if tenant else "unknown",
        "status": tenant.status if tenant else "unknown",
        "storage": usage,
        "user_role": ctx.user_role
    }
