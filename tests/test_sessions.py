"""User session (login-issued API keys) checks — pure DB logic, no services."""
from datetime import datetime, timedelta

from core.db.system_db import SystemDB
from core.tenant.models import UserInfo


def _mk_user(db: SystemDB, tid: str, username: str, api_key: str) -> UserInfo:
    db.create_tenant({"id": tid, "name": tid, "description": "", "status": "active",
                      "industry_packages": "", "storage_quota_mb": 1000})
    u = UserInfo(id=f"u_{username}", tenant_id=tid, username=username, email=None,
                 role="user", api_key=api_key, created_at=datetime.now())
    assert db.create_user(u, "password_hash")
    return u


def test_create_and_resolve_session(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    u = _mk_user(db, "t1", "alice", "ak_primary")
    assert db.create_user_session(u.id, "sess1", "ak_session", None)
    got = db.get_user_by_session_key("ak_session")
    assert got is not None
    assert got.id == u.id
    assert got.username == "alice"
    assert got.tenant_id == "t1"
    # primary key is not a session key
    assert db.get_user_by_session_key("ak_primary") is None


def test_session_expiry_overrides_user_expiry(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    u = _mk_user(db, "t1", "bob", "ak_primary")
    expires = datetime.now() + timedelta(days=7)
    assert db.create_user_session(u.id, "sess1", "ak_session", expires)
    got = db.get_user_by_session_key("ak_session")
    assert got is not None
    assert got.api_key_expires_at is not None
    assert abs((got.api_key_expires_at - expires).total_seconds()) < 5


def test_delete_session_by_key(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    u = _mk_user(db, "t1", "carol", "ak_primary")
    assert db.create_user_session(u.id, "sess1", "ak_session", None)
    assert db.delete_session_by_key("ak_session") is True
    assert db.get_user_by_session_key("ak_session") is None
    # deleting a non-session key reports no revocation
    assert db.delete_session_by_key("ak_primary") is False


def test_delete_sessions_by_user(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    u = _mk_user(db, "t1", "dave", "ak_primary")
    db.create_user_session(u.id, "s1", "ak_s1", None)
    db.create_user_session(u.id, "s2", "ak_s2", None)
    assert db.delete_sessions_by_user(u.id) == 2
    assert db.get_user_by_session_key("ak_s1") is None
    assert db.get_user_by_session_key("ak_s2") is None
