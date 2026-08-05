"""
Tenant and User data models
Independent module, avoids circular imports
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


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
    email: Optional[str]
    role: str
    api_key: Optional[str]
    created_at: datetime
    password_hash: Optional[str] = None  # Internal use only, not returned by API
    api_key_expires_at: Optional[datetime] = None  # None = never expires
