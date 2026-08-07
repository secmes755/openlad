"""
Tenant and User data models
Independent module, avoids circular imports
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TenantInfo:
    """Tenant information"""
    id: str
    name: str
    description: str
    status: str
    industry_packages: list
    storage_quota_mb: int
    created_at: datetime
    updated_at: datetime


@dataclass
class UserInfo:
    """User information"""
    id: str
    tenant_id: str
    username: str
    email: str | None
    role: str
    api_key: str | None
    created_at: datetime
    password_hash: str | None = None  # Internal use only, not returned by API
    api_key_expires_at: datetime | None = None  # None = never expires
