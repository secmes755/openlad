"""
Tenant Manager
Responsible for tenant creation, deletion, querying, and data directory management
"""
import logging
import uuid
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..config import settings
from .models import TenantInfo

logger = logging.getLogger(__name__)


class TenantManager:
    """Tenant Manager
    
    Each tenant has an independent data directory:
    data/tenants/{tenant_id}/
      - metadata.db       (SQLite metadata + FTS5 + structure index)
      - vectors.vec.db    (sqlite-vec L2 page vectors)
      - documents/        (original documents)
      - images/           (page screenshots, figures)
    """

    def __init__(self):
        from ..db.system_db import get_system_db
        self.system_db = get_system_db()
        self._ensure_system_schema()

    def _ensure_system_schema(self):
        pass  # SystemDB initialization is handled automatically in get_system_db()

    def create_tenant(self, name: str, description: str = "",
                      industry_packages: List[str] = None,
                      storage_quota_mb: int = None,
                      tenant_id: str = None) -> TenantInfo:
        """Create new tenant

        Supports custom tenant_id (e.g. admin, u001), otherwise auto-generates UUID.
        """
        tid = tenant_id if tenant_id else str(uuid.uuid4())
        # Check if tenant_id already exists
        existing = self.system_db.get_tenant(tid)
        if existing:
            raise ValueError(f"Tenant ID already exists: {tid}")

        quota = storage_quota_mb or settings.TENANT_CONFIG["default_storage_quota_mb"]

        # Create data directory
        tenant_dir = settings.get_tenant_data_dir(tid)
        for subdir in ["documents", "images"]:
            (tenant_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Initialize tenant database
        from ..db.tenant_db import TenantDBFactory
        TenantDBFactory.init_tenant_databases(tid)

        # Write to system database
        now = datetime.now()
        info = TenantInfo(
            id=tid,
            name=name,
            description=description,
            status="active",
            industry_packages=industry_packages or [],
            storage_quota_mb=quota,
            created_at=now,
            updated_at=now,
        )
        self.system_db.create_tenant(info)
        logger.info(f"[TENANT] Tenant created successfully: {tid} ({name})")
        return info

    def delete_tenant(self, tenant_id: str, hard_delete: bool = False) -> bool:
        """Delete tenant
        
        soft_delete: mark as deleted, preserve data
        hard_delete: permanently remove data directory
        """
        if hard_delete:
            tenant_dir = settings.get_tenant_data_dir(tenant_id)
            if tenant_dir.exists():
                shutil.rmtree(tenant_dir)
                logger.info(f"[TENANT] Deleted tenant data directory: {tenant_id}")

        self.system_db.update_tenant_status(tenant_id, "deleted")
        return True

    def reactivate_tenant(self, tenant_id: str) -> Optional[TenantInfo]:
        """Reactivate a soft-deleted tenant so it can be reused.

        A tenant record marked ``deleted`` may have had its data directory
        removed (hard delete) or kept (soft delete). Recreate the directory
        and databases, then mark the tenant active again. Used e.g. when a
        user is re-created after their independent tenant was deleted — the
        recreated user's API key must work against an active tenant.
        """
        existing = self.system_db.get_tenant(tenant_id)
        if not existing:
            raise ValueError(f"Tenant not found: {tenant_id}")
        if existing.status == "active":
            return existing

        from ..db.tenant_db import TenantDBFactory
        tenant_dir = settings.get_tenant_data_dir(tenant_id)
        for subdir in ["documents", "images"]:
            (tenant_dir / subdir).mkdir(parents=True, exist_ok=True)
        TenantDBFactory.init_tenant_databases(tenant_id)
        self.system_db.update_tenant_status(tenant_id, "active")
        logger.info(f"[TENANT] Reactivated tenant: {tenant_id}")
        return self.system_db.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> Optional[TenantInfo]:
        """Get tenant info"""
        return self.system_db.get_tenant(tenant_id)

    def list_tenants(self, include_deleted: bool = False) -> List[TenantInfo]:
        """List all tenants"""
        return self.system_db.list_tenants(include_deleted=include_deleted)

    def update_tenant(self, tenant_id: str, **kwargs) -> bool:
        """Update tenant info"""
        return self.system_db.update_tenant(tenant_id, **kwargs)

    def get_tenant_storage_usage(self, tenant_id: str) -> Dict[str, Any]:
        """Get tenant storage usage"""
        tenant_dir = settings.get_tenant_data_dir(tenant_id)
        if not tenant_dir.exists():
            return {"total_mb": 0, "documents_mb": 0, "databases_mb": 0, "images_mb": 0}

        total = 0
        docs_size = 0
        db_size = 0
        img_size = 0

        for path in tenant_dir.rglob("*"):
            if path.is_file():
                size = path.stat().st_size
                total += size
                rel = path.relative_to(tenant_dir)
                if str(rel).startswith("documents"):
                    docs_size += size
                elif str(rel).startswith("images"):
                    img_size += size
                elif path.suffix in [".db", ".duckdb"]:
                    db_size += size

        return {
            "total_mb": round(total / 1024 / 1024, 2),
            "documents_mb": round(docs_size / 1024 / 1024, 2),
            "databases_mb": round(db_size / 1024 / 1024, 2),
            "images_mb": round(img_size / 1024 / 1024, 2),
        }


# Singleton
_tenant_manager: Optional[TenantManager] = None


def get_tenant_manager() -> TenantManager:
    global _tenant_manager
    if _tenant_manager is None:
        _tenant_manager = TenantManager()
    return _tenant_manager
