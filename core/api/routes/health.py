"""
Health check routes
"""
import logging

from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger(__name__)


def _check_db() -> dict:
    """Probe system database"""
    try:
        from ...db.system_db import get_system_db
        db = get_system_db()
        with db.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}
    except Exception as e:
        logger.warning(f"DB health check failed: {e}")
        return {"status": "error", "detail": str(e)}


def _check_model_service(url: str, name: str) -> dict:
    """Lightweight probe to check if model service is reachable"""
    try:
        import requests
        # For OpenAI-compatible endpoints, try GET /models (commonly supported)
        resp = requests.get(f"{url}/models", timeout=3)
        if resp.status_code in (200, 401, 404):
            # 401/404 means service is running, just different endpoint
            return {"status": "ok", "http_status": resp.status_code}
        return {"status": "degraded", "http_status": resp.status_code}
    except requests.ConnectionError:
        return {"status": "unreachable"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/health")
async def health_check():
    from ...services.model_config import get_model_settings

    db_status = _check_db()
    cfg = get_model_settings()
    llm_status = _check_model_service(cfg["llm_url"], "llm")
    emb_status = _check_model_service(cfg["emb_url"], "embedding")

    overall = "ok"
    if db_status["status"] != "ok":
        overall = "degraded"
    if llm_status["status"] not in ("ok", "degraded"):
        overall = "degraded"

    return {
        "status": overall,
        "version": "1.0.0",
        "name": "OpenLAD",
        "services": {
            "database": db_status,
            "llm": llm_status,
            "embedding": emb_status,
        }
    }


@router.get("/capacity")
async def capacity_info():
    """Get system capacity information (hardware resources + max tenants)"""
    from ...services.resource_capacity import get_capacity_manager
    capacity_mgr = get_capacity_manager()
    return capacity_mgr.to_dict()


@router.get("/stats")
async def system_stats():
    """System statistics"""
    from ...db.tenant_db import get_tenant_metadata_db
    from ...plugins import get_plugin_registry
    from ...tenant.context import get_tenant_context
    from ...tenant.tenant_manager import get_tenant_manager

    registry = get_plugin_registry()
    tenant_mgr = get_tenant_manager()

    # FIX: Supplement current tenant's document and page statistics
    total_documents = 0
    total_pages = 0
    ctx = get_tenant_context()
    if ctx:
        try:
            db = get_tenant_metadata_db(ctx.tenant_id)
            total_documents = db.count_documents()
            total_pages = db.count_pages()
        except Exception as e:
            logger.warning(f"[STATS] Tenant statistics query failed: {e}")

    return {
        "version": "1.0.0",
        "industry_packages": len(registry.list_plugins()),
        "tenants": len(tenant_mgr.list_tenants()),
        "total_documents": total_documents,
        "total_pages": total_pages,
    }


# =============================================================================
# Service management API
# =============================================================================

def _require_admin():
    """Verify current request user is admin"""
    from ...tenant.context import get_tenant_context
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")


@router.get("/services/status")
async def services_status():
    """Get HTTP health status of external model services (any authenticated user)"""
    from ...services.manager import get_service_manager

    mgr = get_service_manager()
    statuses = mgr.get_status()
    return {
        "services": {
            key: {
                "name": s.name,
                "url": s.url,
                "status": s.status,
                "http_status": s.http_status,
                "last_error": s.last_error,
            }
            for key, s in statuses.items()
        },
        "mode": "external",
        "note": "OpenLAD does not manage model processes — please deploy llama-server / vLLM / Ollama backend yourself"
    }


@router.get("/services/events")
async def service_events(service: str = None, event_type: str = None,
                         limit: int = 100, since_hours: int = 24):
    """Query service event logs (admin only)"""
    _require_admin()
    from ...services.manager import get_service_manager

    mgr = get_service_manager()
    logs = mgr.get_logs(service, limit, event_type, since_hours)
    return {"events": logs, "count": len(logs)}
