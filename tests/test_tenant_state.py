"""Tenant lifecycle state transitions against a temp database."""
from datetime import datetime

from core.db.system_db import SystemDB
from core.tenant.models import TenantInfo


def _tenant(tid, status="active"):
    now = datetime.now()
    return TenantInfo(id=tid, name=tid, description="", status=status,
                      industry_packages=[], storage_quota_mb=100,
                      created_at=now, updated_at=now)


def test_tenant_state_transitions(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    assert db.create_tenant(_tenant("t1"))
    got = db.get_tenant("t1")
    assert got is not None
    assert got.status == "active"
    assert db.update_tenant_status("t1", "deleted")
    got = db.get_tenant("t1")
    assert got is not None and got.status == "deleted"
    assert db.update_tenant_status("t1", "active")
    got = db.get_tenant("t1")
    assert got is not None and got.status == "active"


def test_list_tenants_excludes_deleted(tmp_path):
    db = SystemDB(db_path=tmp_path / "system.db")
    db.create_tenant(_tenant("active1"))
    db.create_tenant(_tenant("gone1", status="deleted"))
    ids = {t.id for t in db.list_tenants()}
    assert "active1" in ids
    assert "gone1" not in ids
