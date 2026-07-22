"""
Rate limiting middleware
Memory-based sliding window, rate limiting by tenant + path category
"""
import logging
import time
from typing import Dict, List

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ...services.resource_capacity import get_capacity_manager

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware

    Dynamically adjusts rate limit quotas based on system capacity:
    - Query type: query_per_minute (dynamically computed, default 4-15/min)
    - Upload type: upload_per_minute (dynamically computed, default 2-5/min)

    Lower capacity → higher per-user quota (fewer users, can be more lenient);
    Higher capacity → lower per-user quota (more users, must be stricter).
    """

    def __init__(self, app):
        super().__init__(app)
        # In-memory request records: {key: [timestamp, ...]}
        self._records: Dict[str, List[float]] = {}

    def _get_limits(self):
        """Dynamically retrieve rate limit quotas"""
        try:
            capacity_mgr = get_capacity_manager()
            limits = capacity_mgr.get_rate_limits()
            return limits.get("query_per_minute", 6), limits.get("upload_per_minute", 2)
        except Exception:
            return 6, 2  # Fallback default values

    def _get_limit_key(self, request: Request) -> tuple:
        """Returns (rate_key, limit_count) or (None, None) meaning no rate limit"""
        path = request.url.path
        # Use authenticated tenant context (set by TenantMiddleware), not raw header
        try:
            from ...tenant.context import get_tenant_context
            ctx = get_tenant_context()
            tenant_id = ctx.tenant_id if ctx else "unknown"
        except Exception:
            tenant_id = "unknown"
        query_limit, upload_limit = self._get_limits()

        if path in ("/api/v1/query", "/api/v1/skill/query", "/api/v1/skill/search"):
            return f"query:{tenant_id}", query_limit
        if path == "/api/v1/documents/upload":
            return f"upload:{tenant_id}", upload_limit
        return None, None

    def _is_allowed(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.time()
        if key not in self._records:
            self._records[key] = []
        # Clean expired records
        self._records[key] = [ts for ts in self._records[key] if now - ts < window_seconds]
        if len(self._records[key]) >= limit:
            return False
        self._records[key].append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        key, limit = self._get_limit_key(request)
        if key is not None:
            if not self._is_allowed(key, limit):
                logger.warning(f"[RATE_LIMIT] Rate limit hit: {key}, limit={limit}")
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "Rate limit exceeded",
                        "detail": f"Too many requests, please try again later. Current limit: {limit}/minute",
                        "retry_after": 60
                    }
                )
        return await call_next(request)
