"""
Global System Database
Manages tenants, users, industry package registry, and global configuration
"""
import logging
import sqlite3
import contextlib
import json
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Generator
from datetime import datetime

from ..config import settings
from ..tenant.models import TenantInfo, UserInfo

logger = logging.getLogger(__name__)


class SystemDB:
    """Global System Database Manager"""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or settings.SYSTEM_DB_PATH
        self.init_schema()

    @contextlib.contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Tenant table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'active',
                    industry_packages TEXT,
                    storage_quota_mb INTEGER DEFAULT 10240,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # User table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    username TEXT NOT NULL,
                    email TEXT,
                    password_hash TEXT,
                    role TEXT DEFAULT 'user',
                    api_key TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Industry package registry table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS industry_packages (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT,
                    path TEXT NOT NULL,
                    category_mapping TEXT,
                    is_builtin INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Global configuration table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Upload task status table (V4.7: replaces in-memory _upload_tasks)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS upload_tasks (
                    task_id TEXT PRIMARY KEY,
                    doc_id TEXT,
                    tenant_id TEXT,
                    filename TEXT,
                    status TEXT DEFAULT 'pending',
                    progress INTEGER DEFAULT 0,
                    message TEXT,
                    result TEXT,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_upload_tasks_tenant ON upload_tasks(tenant_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_upload_tasks_status ON upload_tasks(status)")

            # Indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

            conn.commit()

    # === Tenant Operations ===
    def create_tenant(self, info: TenantInfo) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO tenants (id, name, description, status, industry_packages, storage_quota_mb, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (info.id, info.name, info.description, info.status,
                      json.dumps(info.industry_packages), info.storage_quota_mb,
                      info.created_at, info.updated_at))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to create tenant: {e}")
            return False

    def get_tenant(self, tenant_id: str) -> Optional[TenantInfo]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row:
                return self._row_to_tenant(row)
        return None

    def list_tenants(self, include_deleted: bool = False) -> List[TenantInfo]:
        query = "SELECT * FROM tenants"
        if not include_deleted:
            query += " WHERE status != 'deleted'"
        query += " ORDER BY created_at DESC"
        with self.get_connection() as conn:
            return [self._row_to_tenant(r) for r in conn.execute(query).fetchall()]

    def update_tenant(self, tenant_id: str, **kwargs) -> bool:
        allowed = {"name", "description", "status", "industry_packages", "storage_quota_mb"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values())
        # Handle JSON field
        if "industry_packages" in updates and isinstance(updates["industry_packages"], list):
            values[list(updates.keys()).index("industry_packages")] = json.dumps(updates["industry_packages"])
        values.append(tenant_id)
        try:
            with self.get_connection() as conn:
                conn.execute(f"UPDATE tenants SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to update tenant: {e}")
            return False

    def update_tenant_status(self, tenant_id: str, status: str) -> bool:
        return self.update_tenant(tenant_id, status=status)

    def _row_to_tenant(self, row) -> TenantInfo:
        return TenantInfo(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            status=row["status"],
            industry_packages=json.loads(row["industry_packages"]) if row["industry_packages"] else [],
            storage_quota_mb=row["storage_quota_mb"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else datetime.now(),
        )

    # === User Operations ===
    def create_user(self, user: UserInfo, password_hash: str = None) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO users (id, tenant_id, username, email, password_hash, role, api_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (user.id, user.tenant_id, user.username, user.email,
                      password_hash, user.role, user.api_key, user.created_at))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to create user: {e}")
            return False

    def authenticate_user(self, username: str, password_hash: str,
                           tenant_id: str = None) -> Optional[UserInfo]:
        query = "SELECT * FROM users WHERE username = ? AND password_hash = ?"
        params = [username, password_hash]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        with self.get_connection() as conn:
            row = conn.execute(query, params).fetchone()
            if row:
                return self._row_to_user(row)
        return None

    def find_users_by_username(self, username: str,
                                tenant_id: str = None) -> list:
        """Find users by username (without password check, for bcrypt verification use)"""
        query = "SELECT * FROM users WHERE username = ?"
        params = [username]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        with self.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_user(r) for r in rows]

    def get_user_by_api_key(self, api_key: str) -> Optional[UserInfo]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE api_key = ?",
                (api_key,)
            ).fetchone()
            if row:
                return self._row_to_user(row)
        return None

    def get_user(self, user_id: str) -> Optional[UserInfo]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row:
                return self._row_to_user(row)
        return None

    def list_users(self, tenant_id: str) -> List[UserInfo]:
        with self.get_connection() as conn:
            return [self._row_to_user(r) for r in conn.execute(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
            ).fetchall()]

    def list_all_users(self) -> List[UserInfo]:
        """List all users (across all tenants)"""
        with self.get_connection() as conn:
            return [self._row_to_user(r) for r in conn.execute(
                "SELECT * FROM users ORDER BY created_at DESC"
            ).fetchall()]

    def delete_user(self, user_id: str) -> bool:
        """Delete user"""
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to delete user: {e}")
            return False

    def update_user(self, user_id: str, **kwargs) -> bool:
        """Update user info (role, email, password_hash)"""
        allowed = {"role", "email", "password_hash"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values())
        values.append(user_id)
        try:
            with self.get_connection() as conn:
                conn.execute(f"UPDATE users SET {sets} WHERE id = ?", values)
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to update user: {e}")
            return False

    def update_user_api_key(self, user_id: str, new_api_key: str) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute(
                    "UPDATE users SET api_key = ? WHERE id = ?",
                    (new_api_key, user_id)
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to update API Key: {e}")
            return False

    def _row_to_user(self, row) -> UserInfo:
        return UserInfo(
            id=row["id"],
            tenant_id=row["tenant_id"],
            username=row["username"],
            email=row["email"],
            role=row["role"],
            api_key=row["api_key"],
            password_hash=row["password_hash"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
        )

    # === Industry Package Registry ===
    def register_industry_package(self, pkg_id: str, name: str, version: str,
                                   path: str, category_mapping: list,
                                   is_builtin: bool = False) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO industry_packages
                    (id, name, version, path, category_mapping, is_builtin, is_active, loaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                """, (pkg_id, name, version, path, json.dumps(category_mapping), int(is_builtin)))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to register industry package: {e}")
            return False

    def get_industry_packages(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM industry_packages WHERE is_active = 1").fetchall()
            return [{
                "id": r["id"], "name": r["name"], "version": r["version"],
                "path": r["path"], "category_mapping": json.loads(r["category_mapping"]) if r["category_mapping"] else [],
                "is_builtin": bool(r["is_builtin"]),
            } for r in rows]

    # === System Config ===
    def get_config(self, key: str, default: str = None) -> Optional[str]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM system_config WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_config(self, key: str, value: str) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO system_config (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (key, value))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[SYSTEM_DB] Failed to set config: {e}")
            return False

    # === Upload Task Status (V4.7: replaces in-memory _upload_tasks) ===
    def create_upload_task(self, task_id: str, doc_id: str, filename: str, tenant_id: str = "") -> str:
        """Create upload task record in DB"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO upload_tasks (task_id, doc_id, tenant_id, filename, status, progress, message)
                VALUES (?, ?, ?, ?, 'pending', 0, 'Waiting for processing')
            """, (task_id, doc_id, tenant_id, filename))
            conn.commit()
            return task_id

    def update_upload_task(self, task_id: str, **kwargs) -> bool:
        """Update upload task status in DB. Dict values are JSON-serialized."""
        import json as _json
        allowed_fields = {'status', 'progress', 'message', 'result', 'error'}
        updates = {}
        for k, v in kwargs.items():
            if k not in allowed_fields:
                continue
            if isinstance(v, dict):
                updates[k] = _json.dumps(v, ensure_ascii=False)
            else:
                updates[k] = v
        if not updates:
            return False
        updates['updated_at'] = time.time()
        set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values())
        values.append(task_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE upload_tasks SET {set_clause} WHERE task_id = ?
            """, values)
            conn.commit()
            return cursor.rowcount > 0

    def get_upload_task(self, task_id: str) -> Optional[Dict]:
        """Get upload task by ID. JSON fields are deserialized."""
        import json as _json
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM upload_tasks WHERE task_id = ?
            """, (task_id,))
            row = cursor.fetchone()
            if row:
                task = dict(row)
                # Deserialize JSON fields
                for field in ('result',):
                    if task.get(field) and isinstance(task[field], str):
                        try:
                            task[field] = _json.loads(task[field])
                        except (ValueError, TypeError):
                            pass
                return task
            return None

    def cleanup_upload_tasks(self, max_age_hours: int = 24) -> int:
        """Clean up old completed/failed tasks"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM upload_tasks 
                WHERE status IN ('completed', 'failed', 'error')
                AND updated_at < datetime('now', '-{} hours')
            """.format(max_age_hours))
            conn.commit()
            return cursor.rowcount

    def restore_interrupted_tasks(self) -> List[Dict]:
        """Restore tasks that were interrupted (processing status)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM upload_tasks WHERE status = 'processing'
            """)
            return [dict(row) for row in cursor.fetchall()]


# Singleton
_system_db: Optional[SystemDB] = None
_system_db_lock = threading.Lock()


def get_system_db() -> SystemDB:
    global _system_db
    if _system_db is None:
        with _system_db_lock:
            if _system_db is None:
                _system_db = SystemDB()
    return _system_db
