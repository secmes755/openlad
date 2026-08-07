"""
Rate limiting middleware
Memory-based sliding window, rate limiting by tenant + path category
"""
import json
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ...services.resource_capacity import get_capacity_manager

logger = logging.getLogger(__name__)

# Login rate limit defaults (per minute). Overridable via settings.LOGIN_RATE_LIMIT.
# Internal-network posture: lenient, throttle only — never lock the account.
LOGIN_USER_PER_MINUTE = 5
LOGIN_IP_PER_MINUTE = 20


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
        self._records: dict[str, list[float]] = {}

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

    def _get_login_limits(self):
        """Login rate limits (username / IP per minute), overridable via settings."""
        user_lim, ip_lim = LOGIN_USER_PER_MINUTE, LOGIN_IP_PER_MINUTE
        try:
            from ...config import settings
            cfg = getattr(settings, "LOGIN_RATE_LIMIT", None)
            if isinstance(cfg, dict):
                user_lim = int(cfg.get("username_per_minute", user_lim))
                ip_lim = int(cfg.get("ip_per_minute", ip_lim))
        except Exception:
            pass
        return user_lim, ip_lim

    def _login_throttle_response(self, retry_after: int = 60):
        return JSONResponse(
            status_code=429,
            content={
                "error": "Too many login attempts",
                "detail": "Login rate limit exceeded. Please try again later (account is NOT locked).",
                "retry_after": retry_after,
            },
        )

    async def _handle_login_rate_limit(self, request: Request, call_next):
        """Dual-track login throttle: per-username AND per-IP. Throttle only, no lockout."""
        body = await request.body()
        # Cache body so downstream route handler can read it
        request._body = body
        username = None
        try:
            username = json.loads(body.decode("utf-8", "ignore") or "{}").get("username")
        except Exception:
            username = None
        ip = request.client.host if request.client else "unknown"

        user_lim, ip_lim = self._get_login_limits()
        # Per-username track (only when a username is present)
        if username and not self._is_allowed(f"login_user:{username}", user_lim):
            logger.warning(f"[RATE_LIMIT] Login throttle (username={username}, limit={user_lim}/min)")
            return self._login_throttle_response()
        # Per-IP track
        if not self._is_allowed(f"login_ip:{ip}", ip_lim):
            logger.warning(f"[RATE_LIMIT] Login throttle (ip={ip}, limit={ip_lim}/min)")
            return self._login_throttle_response()
        return await call_next(request)

    async def dispatch(self, request: Request, call_next):
        # Login endpoint: dual-track throttle (runs before TenantMiddleware, no tenant context)
        if request.url.path == "/api/v1/login" and request.method == "POST":
            return await self._handle_login_rate_limit(request, call_next)

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
