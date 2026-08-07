"""
OpenLAD Diagnostic API
Provides system-level diagnostic functions, including:
- User listing
- Document hierarchy (by tenant + category)
- Tenant information
- Database health check
"""
import logging
import os
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...config import settings
from ...db.system_db import get_system_db
from ...tenant.context import get_tenant_context

logger = logging.getLogger(__name__)
router = APIRouter()


class UserInfo(BaseModel):
    username: str
    role: str
    tenant_id: str
    api_key_prefix: str
    created_at: str | None


class DocumentInfo(BaseModel):
    id: str
    title: str
    filename: str
    doc_type: str
    status: str
    page_count: int | None
    chunk_count: int | None
    category_level1: str | None
    category_level2: str | None
    category_level3: str | None
    tenant_id: str
    tenant_name: str | None
    created_at: str | None
    updated_at: str | None


class TenantInfo(BaseModel):
    tenant_id: str
    name: str
    description: str | None
    document_count: int
    user_count: int
    storage_used_mb: float | None


class CategoryNode(BaseModel):
    name: str
    level: int
    document_count: int
    children: list[Any]  # Recursive type, use Any to avoid Pydantic recursion issues
    documents: list[DocumentInfo]


class DiagnosticResponse(BaseModel):
    status: str
    timestamp: str
    data: dict[str, Any]


@router.get("/diagnostic/users")
async def list_all_users():
    """List all users in the system (requires admin privileges)"""
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    try:
        get_system_db()
        # Query all users directly from SQLite
        db_path = str(settings.SYSTEM_DB_PATH)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT username, role, tenant_id, api_key, created_at
            FROM users
            ORDER BY tenant_id, username
        """)

        users = []
        for row in cursor.fetchall():
            users.append({
                "username": row["username"],
                "role": row["role"],
                "tenant_id": row["tenant_id"],
                "api_key_prefix": row["api_key"][:8] + "..." if row["api_key"] else None,
                "created_at": row["created_at"]
            })

        conn.close()

        return {
            "status": "ok",
            "count": len(users),
            "users": users
        }
    except Exception as e:
        logger.error(f"[DIAGNOSTIC] Query users failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Query users failed: {str(e)}")


@router.get("/diagnostic/tenants")
async def list_all_tenants():
    """List all tenants and their statistics"""
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    try:
        get_system_db()
        db_path = str(settings.SYSTEM_DB_PATH)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get all tenants
        cursor.execute("SELECT id, name, description, storage_quota_mb FROM tenants")
        tenants = []

        for row in cursor.fetchall():
            tenant_id = row["id"]

            # Count documents for this tenant
            doc_count = 0
            try:
                metadata_db_path = str(settings.TENANTS_DIR / tenant_id / "metadata.db")
                if os.path.exists(metadata_db_path):
                    meta_conn = sqlite3.connect(metadata_db_path)
                    meta_cursor = meta_conn.cursor()
                    meta_cursor.execute("SELECT COUNT(*) FROM documents")
                    doc_count = meta_cursor.fetchone()[0]
                    meta_conn.close()
            except Exception:
                pass

            # Count users for this tenant
            cursor.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tenant_id,))
            user_count = cursor.fetchone()[0]

            tenants.append({
                "tenant_id": tenant_id,
                "name": row["name"],
                "description": row["description"],
                "document_count": doc_count,
                "user_count": user_count,
                "storage_quota_mb": row["storage_quota_mb"]
            })

        conn.close()

        return {
            "status": "ok",
            "count": len(tenants),
            "tenants": tenants
        }
    except Exception as e:
        logger.error(f"[DIAGNOSTIC] Query tenants failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Query tenants failed: {str(e)}")


@router.get("/diagnostic/documents")
async def list_all_documents(
    tenant_id: str | None = None,
    category: str | None = None
):
    """
    List all documents, organized by tenant and category hierarchy
    - tenant_id: filter by specific tenant
    - category: filter by specific category (supports level1/level2/level3)
    """
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    try:
        # Get all tenants or specified tenant
        target_tenants = []
        if tenant_id:
            target_tenants = [tenant_id]
        else:
            db_path = str(settings.SYSTEM_DB_PATH)
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tenants")
            target_tenants = [row[0] for row in cursor.fetchall()]
            conn.close()

        all_documents = []
        tenant_stats = {}

        for tid in target_tenants:
            metadata_db_path = str(settings.TENANTS_DIR / tid / "metadata.db")

            if not os.path.exists(metadata_db_path):
                continue

            try:
                meta_conn = sqlite3.connect(metadata_db_path)
                meta_conn.row_factory = sqlite3.Row
                meta_cursor = meta_conn.cursor()

                # Get tenant name
                system_db_path = str(settings.SYSTEM_DB_PATH)
                sys_conn = sqlite3.connect(system_db_path)
                sys_conn.row_factory = sqlite3.Row
                sys_cursor = sys_conn.cursor()
                sys_cursor.execute("SELECT name FROM tenants WHERE id = ?", (tid,))
                tenant_row = sys_cursor.fetchone()
                tenant_name = tenant_row["name"] if tenant_row else tid
                sys_conn.close()

                # Build query conditions
                query = """
                    SELECT id, title, filename, doc_type, status,
                           category_level1, category_level2, category_level3,
                           metadata_json, created_at, updated_at
                    FROM documents
                    WHERE 1=1
                """
                params = []

                if category:
                    # Support filtering by any category level
                    query += " AND (category_level1 = ? OR category_level2 = ? OR category_level3 = ?)"
                    params.extend([category, category, category])

                query += " ORDER BY category_level1, category_level2, category_level3, title"

                meta_cursor.execute(query, params)

                for row in meta_cursor.fetchall():
                    # Parse metadata_json to get page count
                    page_count = None
                    chunk_count = None
                    try:
                        metadata = json.loads(row["metadata_json"] or "{}")
                        page_count = metadata.get("num_pages")
                    except Exception:
                        pass

                    # Get chunk count
                    try:
                        meta_cursor.execute(
                            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = ?",
                            (row["id"],)
                        )
                        chunk_count = meta_cursor.fetchone()[0]
                    except Exception:
                        pass

                    doc_info = {
                        "id": row["id"],
                        "title": row["title"] or "Untitled",
                        "filename": row["filename"],
                        "doc_type": row["doc_type"],
                        "status": row["status"],
                        "page_count": page_count,
                        "chunk_count": chunk_count,
                        "category_level1": row["category_level1"],
                        "category_level2": row["category_level2"],
                        "category_level3": row["category_level3"],
                        "tenant_id": tid,
                        "tenant_name": tenant_name,
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"]
                    }
                    all_documents.append(doc_info)

                tenant_stats[tid] = {
                    "name": tenant_name,
                    "document_count": len([d for d in all_documents if d["tenant_id"] == tid])
                }

                meta_conn.close()
            except Exception as e:
                logger.warning(f"[DIAGNOSTIC] Failed to query documents for tenant {tid}: {e}")
                continue

        # Build hierarchy structure
        hierarchy = _build_category_hierarchy(all_documents)

        return {
            "status": "ok",
            "total_documents": len(all_documents),
            "tenant_stats": tenant_stats,
            "hierarchy": hierarchy,
            "documents": all_documents
        }
    except Exception as e:
        logger.error(f"[DIAGNOSTIC] Query documents failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Query documents failed: {str(e)}")


def _build_category_hierarchy(documents: list[dict]) -> list[dict]:
    """Build category hierarchy tree"""
    root = {}

    for doc in documents:
        l1 = doc["category_level1"] or "Uncategorized"
        l2 = doc["category_level2"] or "Uncategorized"
        l3 = doc["category_level3"] or "Uncategorized"

        if l1 not in root:
            root[l1] = {"name": l1, "level": 1, "document_count": 0, "children": {}, "documents": []}
        root[l1]["document_count"] += 1

        if l2 not in root[l1]["children"]:
            root[l1]["children"][l2] = {"name": l2, "level": 2, "document_count": 0, "children": {}, "documents": []}
        root[l1]["children"][l2]["document_count"] += 1

        if l3 not in root[l1]["children"][l2]["children"]:
            root[l1]["children"][l2]["children"][l3] = {"name": l3, "level": 3, "document_count": 0, "documents": []}
        root[l1]["children"][l2]["children"][l3]["document_count"] += 1

        # Add document to deepest level
        root[l1]["children"][l2]["children"][l3]["documents"].append({
            "id": doc["id"],
            "title": doc["title"],
            "tenant_id": doc["tenant_id"],
            "tenant_name": doc["tenant_name"],
            "page_count": doc["page_count"],
            "chunk_count": doc["chunk_count"],
            "status": doc["status"]
        })

    # Convert to list structure
    def convert_to_list(node_dict):
        result = []
        for key in sorted(node_dict.keys()):
            node = node_dict[key]
            converted = {
                "name": node["name"],
                "level": node["level"],
                "document_count": node["document_count"],
                "documents": node.get("documents", [])
            }
            if "children" in node and node["children"]:
                converted["children"] = convert_to_list(node["children"])
            result.append(converted)
        return result

    return convert_to_list(root)


@router.get("/diagnostic/health")
async def system_health():
    """System health check"""
    # Require admin privileges for system-wide health data
    from ...tenant.context import get_tenant_context
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    try:
        health_status = {
            "api": "ok",
            "databases": {},
            "services": {}
        }

        # Check system database
        system_db_path = str(settings.SYSTEM_DB_PATH)
        if os.path.exists(system_db_path):
            try:
                conn = sqlite3.connect(system_db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM users")
                user_count = cursor.fetchone()[0]
                conn.close()
                health_status["databases"]["system_db"] = {
                    "status": "ok",
                    "users": user_count
                }
            except Exception as e:
                health_status["databases"]["system_db"] = {"status": "error", "error": str(e)}
        else:
            health_status["databases"]["system_db"] = {"status": "missing"}

        # Check each tenant database
        tenants_dir = str(settings.TENANTS_DIR)
        if os.path.exists(tenants_dir):
            for tenant_dir in os.listdir(tenants_dir):
                metadata_db = os.path.join(tenants_dir, tenant_dir, "metadata.db")
                if os.path.exists(metadata_db):
                    try:
                        conn = sqlite3.connect(metadata_db)
                        cursor = conn.cursor()
                        cursor.execute("SELECT COUNT(*) FROM documents")
                        doc_count = cursor.fetchone()[0]
                        conn.close()
                        health_status["databases"][f"tenant_{tenant_dir}"] = {
                            "status": "ok",
                            "documents": doc_count
                        }
                    except Exception as e:
                        health_status["databases"][f"tenant_{tenant_dir}"] = {
                            "status": "error",
                            "error": str(e)
                        }

        return {
            "status": "ok",
            "health": health_status
        }
    except Exception as e:
        logger.error(f"[DIAGNOSTIC] Health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")


@router.get("/diagnostic/document/{doc_id}")
async def get_document_detail(doc_id: str, tenant_id: str):
    """Get detailed information for a single document"""
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    try:
        metadata_db_path = str(settings.TENANTS_DIR / tenant_id / "metadata.db")

        if not os.path.exists(metadata_db_path):
            raise HTTPException(status_code=404, detail="Tenant database does not exist")

        conn = sqlite3.connect(metadata_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get document basic info
        cursor.execute("""
            SELECT id, title, filename, doc_type, status,
                   category_level1, category_level2, category_level3,
                   metadata_json, created_at, updated_at
            FROM documents WHERE id = ?
        """, (doc_id,))

        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")

        # Get page statistics
        cursor.execute("SELECT COUNT(*) FROM doc_pages WHERE doc_id = ?", (doc_id,))
        page_count = cursor.fetchone()[0]

        # Get chunk statistics
        cursor.execute("SELECT COUNT(*) FROM doc_chunks WHERE document_id = ?", (doc_id,))
        chunk_count = cursor.fetchone()[0]

        # Get page type distribution
        cursor.execute("""
            SELECT page_type, COUNT(*) as count
            FROM doc_pages
            WHERE doc_id = ?
            GROUP BY page_type
        """, (doc_id,))
        page_types = {row[0]: row[1] for row in cursor.fetchall()}

        # Get section structure
        cursor.execute("""
            SELECT DISTINCT section_path, section_title, page_num
            FROM doc_pages
            WHERE doc_id = ? AND section_path IS NOT NULL
            ORDER BY page_num
        """, (doc_id,))
        sections = []
        for sec_row in cursor.fetchall():
            sections.append({
                "path": sec_row[0],
                "title": sec_row[1],
                "page": sec_row[2]
            })

        conn.close()

        return {
            "status": "ok",
            "document": {
                "id": row["id"],
                "title": row["title"],
                "filename": row["filename"],
                "doc_type": row["doc_type"],
                "status": row["status"],
                "category": {
                    "level1": row["category_level1"],
                    "level2": row["category_level2"],
                    "level3": row["category_level3"]
                },
                "page_count": page_count,
                "chunk_count": chunk_count,
                "page_types": page_types,
                "sections": sections[:50],  # Limit return count
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DIAGNOSTIC] Failed to query document details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to query document details: {str(e)}")


import json  # Add json import at end of file
