"""
Multi-tenant middleware
Identify tenant on each request and set context
"""
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ...tenant.auth import get_auth_manager
from ...tenant.context import TenantContext, clear_tenant_context, set_tenant_context
from ...tenant.tenant_manager import get_tenant_manager

logger = logging.getLogger(__name__)


class TenantMiddleware(BaseHTTPMiddleware):
    """Multi-tenant middleware

    Identify tenant from request headers:
    - X-Tenant-ID: tenant ID
    - Authorization: Bearer *** or username/password
    """

    async def dispatch(self, request: Request, call_next):
        # Skip routes that don't require tenant
        path = request.url.path
        # Health check, root path, login — fully skip
        if path in ["/", "/api/v1/health", "/api/v1/login"]:
            return await call_next(request)
        # Static assets and HTML pages are public, including the admin page
        # shell. The admin page carries no data by itself: every API call it
        # makes (documents/stats/industries/...) sends its own Authorization
        # header and is authenticated below. Page-level auth cannot work
        # here because the frontend opens /static/admin.html via a plain
        # anchor link — browser top-level navigation never sends
        # Authorization headers.
        if path.startswith("/static/") or any(path.endswith(ext) for ext in (".js", ".css", ".png", ".jpg", ".svg", ".ico", ".woff2", ".html", ".json", ".txt", ".md")):
            return await call_next(request)
        # /api/v1/industries is no longer public — requires authentication
        # (industry package listing is not sensitive data but stays behind auth)

        tenant_id = request.headers.get("X-Tenant-ID")
        auth_header = request.headers.get("Authorization", "")

        user_id = None
        user_role = None
        api_key = None
        user = None

        # API Key authentication (non-exempt paths require authentication)
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "Authentication required", "detail": "Please provide Authorization: Bearer ***"}
            )

        api_key = auth_header.replace("Bearer ", "").strip()
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"error": "API Key cannot be empty"}
            )

        auth_mgr = get_auth_manager()
        user = auth_mgr.authenticate_by_api_key(api_key)
        if not user:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid API Key"}
            )

        # API key lifecycle: reject expired keys
        if auth_mgr.is_api_key_expired(user):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "API Key expired",
                    "detail": "This API Key has expired. Please contact an admin to rotate it."
                }
            )

        user_id = user.id
        user_role = user.role
        # If tenant_id not explicitly specified, derive from user association
        if not tenant_id:
            tenant_id = user.tenant_id
        else:
            # Security check: user can only access their own tenant, but admin role can cross-tenant
            if tenant_id != user.tenant_id and user.role != "admin":
                return JSONResponse(
                    status_code=403,
                    content={"error": "No permission to access this tenant"}
                )

        # Check if tenant exists
        tenant_mgr = get_tenant_manager()
        tenant = tenant_mgr.get_tenant(tenant_id)
        if not tenant or tenant.status != "active":
            return JSONResponse(
                status_code=403,
                content={"error": "Tenant not found or inactive"}
            )

        # Set tenant context
        ctx = TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            username=user.username if user else None,
            user_role=user_role,
            api_key=api_key,
        )
        set_tenant_context(ctx)

        try:
            response = await call_next(request)
            return response
        finally:
            clear_tenant_context()
