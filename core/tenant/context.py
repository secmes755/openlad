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
    user_id: Optional[str] = None
    username: Optional[str] = None
    user_role: Optional[str] = None
    api_key: Optional[str] = None
    industry_hint: Optional[str] = None  # Industry package specified by user

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


def get_tenant_context() -> Optional[TenantContext]:
    """Get tenant context for the current thread/coroutine"""
    return _tenant_context.get()


def clear_tenant_context():
    """Clear tenant context"""
    _tenant_context.set(None)
