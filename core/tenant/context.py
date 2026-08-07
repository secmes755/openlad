"""
Tenant Context Management
Provides request-level tenant context with dependency injection support
"""
import contextvars
from dataclasses import dataclass
from typing import Optional

_tenant_context: contextvars.ContextVar[Optional["TenantContext"]] = contextvars.ContextVar(
    "tenant_context", default=None
)


@dataclass
class TenantContext:
    """Tenant context

    Tenant information carried by each request, propagated through the entire processing chain.
    """
    tenant_id: str
    user_id: str | None = None
    username: str | None = None
    user_role: str | None = None
    api_key: str | None = None
    industry_hint: str | None = None  # Industry package specified by user

    def get_data_dir(self):
        from ..config import settings
        return settings.get_tenant_data_dir(self.tenant_id)

    def get_db_path(self):
        from ..config import settings
        return settings.get_tenant_db_path(self.tenant_id)

    def get_vec_db_path(self):
        from ..config import settings
        return settings.get_tenant_vec_db_path(self.tenant_id)

    def get_documents_dir(self):
        from ..config import settings
        return settings.get_tenant_documents_dir(self.tenant_id)

    def get_images_dir(self):
        from ..config import settings
        return settings.get_tenant_images_dir(self.tenant_id)


def set_tenant_context(ctx: TenantContext):
    """Set tenant context for the current thread/coroutine"""
    _tenant_context.set(ctx)


def get_tenant_context() -> TenantContext | None:
    """Get tenant context for the current thread/coroutine"""
    return _tenant_context.get()


def clear_tenant_context():
    """Clear tenant context"""
    _tenant_context.set(None)
