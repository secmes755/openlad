"""DB-level (tenant_id, username) uniqueness guard (backstop for the 409 path)."""
import sqlite3
from datetime import datetime

import pytest

from core.db.system_db import SystemDB
from core.tenant.models import TenantInfo


def _mk(db, tid):
    now = datetime.now()
    db.create_tenant(TenantInfo(id=tid, name=tid, description="", status="active",
                                industry_packages=[], storage_quota_mb=100,
                                created_at=now, updated_at=now))


def _insert(conn, uid, tid, username, api_key):
    conn.execute(
        "INSERT INTO users (id, tenant_id, username, role, api_key) VALUES (?, ?, ?, 'user', ?)",
        (uid, tid, username, api_key))


def test_duplicate_username_same_tenant_rejected(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    _mk(db, "t1")
    conn = sqlite3.connect(tmp_path / "system.db")
    _insert(conn, "u1", "t1", "alice", "ak-1")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "u2", "t1", "alice", "ak-2")
        conn.commit()
    conn.close()


def test_same_username_different_tenant_allowed(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    _mk(db, "t1")
    _mk(db, "t2")
    conn = sqlite3.connect(tmp_path / "system.db")
    _insert(conn, "u1", "t1", "alice", "ak-1")
    _insert(conn, "u2", "t2", "alice", "ak-2")
    conn.commit()
    conn.close()
