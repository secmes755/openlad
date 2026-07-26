"""
Query Routes
External mode: Concurrency strategy controlled by OPENLAD_QUERY_CONCURRENCY_MODE / OPENLAD_QUERY_MAX_CONCURRENT environment variables,
read by resource_capacity.py's get_query_concurrency_config().
"""
import asyncio
import logging
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from ...tenant.context import get_tenant_context
from fastapi import Request

router = APIRouter()
logger = logging.getLogger(__name__)


def _create_query_lock():
    """Create an appropriate concurrency control object based on hardware configuration"""
    from ...services.resource_capacity import get_capacity_manager
    cfg = get_capacity_manager().get_query_concurrency_config()
    if cfg["mode"] == "semaphore":
        lock = asyncio.Semaphore(cfg["max_concurrent"])
        logger.info(
            f"[QUERY] Concurrency mode: Semaphore({cfg['max_concurrent']}) - {cfg['reason']}"
        )
    else:
        lock = asyncio.Lock()
        logger.info(
            f"[QUERY] Concurrency mode: Lock (serial) - {cfg['reason']}"
        )
    return lock


_query_lock = _create_query_lock()

# Follow-up support: when a request carries a session_id but no explicit
# chat_history, load the most recent stored messages so conversation-aware
# rewrite (pronoun resolution) has context. Bounded to keep prompts small.
CHAT_HISTORY_LOAD_LIMIT = 10        # max stored messages loaded per query
CHAT_HISTORY_MSG_MAX_CHARS = 1500   # per-message content cap for history


class QueryRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    industry: Optional[str] = None
    chat_history: Optional[List[dict]] = []


MAX_QUERY_LENGTH = 2000  # Default value, will be read from config in endpoint


class QueryResponse(BaseModel):
    answer: str
    sources: List[dict]
    confidence: str
    elapsed_ms: int


@router.post("/query")
async def query(req: QueryRequest, request: Request):
    """Document query (protected by global concurrency lock)
    
    Concurrency strategy is dynamically determined by hardware configuration:
    - Single GPU / consumer-grade GPU: serial execution (Lock)
    - Multi-GPU / server-grade GPU: limited concurrency allowed (Semaphore)
    If current concurrency limit is reached, subsequent requests will automatically queue.
    """
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    # Query length validation
    from ...config import settings
    max_len = settings.RATE_LIMIT_CONFIG.get("max_query_length", 2000)
    if len(req.query) > max_len:
        raise HTTPException(
            status_code=400,
            detail=f"Query length exceeds limit: {max_len} characters"
        )

    engine = getattr(request.app.state, "query_engine", None)
    if not engine:
        raise HTTPException(status_code=503, detail="Search engine not initialized, please try again later")

    # Follow-up support: session history is write-only unless we load it here.
    # Client-supplied chat_history always takes precedence; otherwise, when a
    # session_id is provided, hydrate history from the stored session so
    # follow-up questions ("what are their differences") can be resolved.
    chat_history = req.chat_history
    if req.session_id and not chat_history:
        from ...db.tenant_db import get_tenant_metadata_db
        try:
            hist_db = get_tenant_metadata_db(ctx.tenant_id)
            # Ownership check (same rule as GET /chat/sessions/{id}/messages):
            # never hydrate another user's conversation into this query.
            with hist_db.get_connection() as conn:
                owned = conn.execute(
                    "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
                    (req.session_id, ctx.user_id)
                ).fetchone()
            if not owned:
                logger.warning(f"[QUERY] session {req.session_id[:8]} not owned by user, skip history hydration")
                stored = []
            else:
                stored = hist_db.get_chat_messages(req.session_id)
            # created_at has second granularity; id order is the true insertion order
            stored = sorted(stored, key=lambda m: m.get("id", 0))
            chat_history = [
                {"role": m.get("role", ""),
                 "content": (m.get("content", "") or "")[:CHAT_HISTORY_MSG_MAX_CHARS]}
                for m in stored[-CHAT_HISTORY_LOAD_LIMIT:]
            ]
            if chat_history:
                logger.info(f"[QUERY] Hydrated {len(chat_history)} messages from session {req.session_id[:8]}")
        except Exception as e:
            logger.warning(f"[QUERY] Failed to load session history: {e}")

    # Global concurrency lock — waiting time reported to user
    wait_start = time.time()
    async with _query_lock:
        wait_ms = int((time.time() - wait_start) * 1000)

        query_start = time.time()
        result = await asyncio.to_thread(
            engine.query,
            query_text=req.query,
            tenant_id=ctx.tenant_id,
            industry_hint=req.industry,
            chat_history=chat_history
        )
        elapsed_ms = int((time.time() - query_start) * 1000)
        
        result["tenant_id"] = ctx.tenant_id
        result["industry"] = req.industry or "auto"
        result["elapsed_ms"] = elapsed_ms
        result["wait_ms"] = wait_ms
        if wait_ms > 1000:
            result["queue_notice"] = f"Current query queued for {wait_ms//1000} seconds, system is processing"

    from ...db.tenant_db import get_tenant_metadata_db
    import json
    db = get_tenant_metadata_db(ctx.tenant_id)

    # Auto-create anonymous session (if no session_id provided)
    session_id = req.session_id
    if not session_id:
        import uuid
        session_id = str(uuid.uuid4())
        db.create_chat_session(session_id, user_id=ctx.user_id, title=req.query[:20], industry=req.industry or "auto")
        result["auto_session_id"] = session_id

    # Save messages to session
    db.save_chat_message(
        session_id=session_id,
        role="user",
        content=req.query,
        query_info=json.dumps({"industry": req.industry or "auto"})
    )
    db.save_chat_message(
        session_id=session_id,
        role="assistant",
        content=result.get("answer", ""),
        sources=json.dumps(result.get("sources", [])),
        query_info=json.dumps({"confidence": result.get("confidence", "none"), "elapsed_ms": elapsed_ms})
    )
    result["session_id"] = session_id

    # Record query log (audit)
    try:
        db.log_query(
            query=req.query,
            user_id=ctx.user_id,
            intent=result.get("plan", {}).get("intent", "unknown"),
            industry_package_id=req.industry,
            elapsed_ms=elapsed_ms,
            results_count=len(result.get("sources", [])),
            answer_length=len(result.get("answer", "")),
            trace={"session_id": session_id, "auto_created": not req.session_id}
        )
    except Exception as e:
        logger.warning(f"Failed to record query log: {e}")

    return result


@router.post("/chat/sessions")
async def create_chat_session(title: Optional[str] = "New conversation", industry: str = "auto"):
    """Create chat session"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    import uuid
    session_id = str(uuid.uuid4())
    from ...db.tenant_db import get_tenant_metadata_db
    db = get_tenant_metadata_db(ctx.tenant_id)
    db.create_chat_session(session_id, user_id=ctx.user_id, title=title, industry=industry)
    return {"id": session_id, "session_id": session_id, "title": title}


@router.get("/chat/sessions")
async def list_chat_sessions():
    """List chat sessions"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    from ...db.tenant_db import get_tenant_metadata_db
    db = get_tenant_metadata_db(ctx.tenant_id)
    sessions = db.get_chat_sessions(user_id=ctx.user_id)
    return {"sessions": sessions}


@router.get("/chat/sessions/{session_id}/messages")
async def get_chat_messages(session_id: str):
    """Get session messages"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    from ...db.tenant_db import get_tenant_metadata_db
    db = get_tenant_metadata_db(ctx.tenant_id)
    # Verify session belongs to current user
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
            (session_id, ctx.user_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = db.get_chat_messages(session_id)
    return {"messages": messages}


@router.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    """Delete chat session"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    from ...db.tenant_db import get_tenant_metadata_db
    db = get_tenant_metadata_db(ctx.tenant_id)
    # Verify session belongs to current user
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
            (session_id, ctx.user_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        with db.get_connection() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
            conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
