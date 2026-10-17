"""Behavioural proof that a promoted user does not stay in its old tenant.

The source-shape tests above catch an accidental revert; this one actually runs
the DB layer against a temp SQLite database and asserts the stored row, which is
what the tenant middleware reads.
"""

import sqlite3

from core.db.system_db import SystemDB

SCHEMA = """
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  username TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL DEFAULT 'user',
  api_key TEXT,
  password_hash TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  api_key_expires_at TEXT
);
"""


def _make_db(tmp_path, monkeypatch):
    db_path = tmp_path / "system.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO users (id, tenant_id, username, role) VALUES (?,?,?,?)",
        ("u_bob", "tenant_b", "bob", "user"),
    )
    conn.commit()
    conn.close()

    db = SystemDB()
    monkeypatch.setattr(db, "get_connection", lambda: sqlite3.connect(db_path))
    return db, db_path


def test_promotion_moves_account_into_admin_tenant(tmp_path, monkeypatch):
    db, db_path = _make_db(tmp_path, monkeypatch)

    ok = db.update_user("u_bob", role="admin", tenant_id="admin")
    assert ok is True

    row = sqlite3.connect(db_path).execute(
        "SELECT role, tenant_id FROM users WHERE id='u_bob'").fetchone()
    assert row == ("admin", "admin"), (
        f"promoted account is role={row[0]!r} tenant={row[1]!r}; it must be "
        "('admin','admin') or the middleware hands cross-tenant rights to an "
        "account whose home tenant is still a normal one"
    )


def test_role_only_update_would_leave_tenant_behind(tmp_path, monkeypatch):
    """Documents why the route must send tenant_id: role alone is not enough."""
    db, db_path = _make_db(tmp_path, monkeypatch)

    db.update_user("u_bob", role="admin")  # no tenant_id

    row = sqlite3.connect(db_path).execute(
        "SELECT role, tenant_id FROM users WHERE id='u_bob'").fetchone()
    assert row == ("admin", "tenant_b"), (
        "DB layer should apply exactly what it is given; the *route* is "
        "responsible for keeping tenant consistent"
    )


def test_unknown_columns_are_ignored(tmp_path, monkeypatch):
    db, db_path = _make_db(tmp_path, monkeypatch)

    db.update_user("u_bob", role="user", evil_column="x'; DROP TABLE users;--")

    row = sqlite3.connect(db_path).execute(
        "SELECT role FROM users WHERE id='u_bob'").fetchone()
    assert row[0] == "user"
    # table must still exist
    tables = sqlite3.connect(db_path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert any(t[0] == "users" for t in tables)
