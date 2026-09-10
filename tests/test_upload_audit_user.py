"""The upload audit row must name the acting user.

Two tests: one pins the platform reason the value cannot simply be read inside
the worker thread, the other pins the fix.

``_process_document_async_sync`` runs on a worker thread dispatched through
``loop.run_in_executor``. Only ``asyncio.to_thread`` copies the caller's
contextvars, so inside that worker the tenant context is empty. The code read
``user_id`` from the tenant context there, which meant every
``action=document_upload`` audit row was written with ``user_id=""`` — the
tenant was still attributed correctly because it is passed as an argument.
"""

import asyncio
import concurrent.futures

from core.api.routes import documents as documents_mod
from core.tenant.context import (
    TenantContext,
    clear_tenant_context,
    get_tenant_context,
    set_tenant_context,
)


class _RecordingDB:
    def __init__(self):
        self.audits = []

    def log_audit(self, **kwargs):
        self.audits.append(kwargs)
        return 1


class _FakeBuilder:
    def ingest_document(self, **kwargs):
        return {"doc_id": "doc-1", "status": "completed"}


def test_run_in_executor_does_not_carry_the_tenant_context():
    """Characterises the constraint the fix works around (true on any thread pool)."""
    set_tenant_context(TenantContext(tenant_id="admin", user_id="alice"))
    try:
        ctx = get_tenant_context()
        assert ctx is not None and ctx.user_id == "alice"

        async def read_in_worker():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return await asyncio.get_event_loop().run_in_executor(
                    pool, get_tenant_context
                )

        assert asyncio.run(read_in_worker()) is None
    finally:
        clear_tenant_context()


def test_upload_audit_records_the_acting_user(monkeypatch):
    from core.db import tenant_db as tenant_db_mod

    recorded = _RecordingDB()
    monkeypatch.setattr(tenant_db_mod, "get_tenant_metadata_db",
                        lambda tenant_id: recorded)
    monkeypatch.setattr(documents_mod, "_update_task", lambda *a, **k: None)

    documents_mod._process_document_async_sync(
        task_id="task-1",
        tenant_id="admin",
        file_path="/tmp/does-not-exist.pdf",
        industry=None,
        auto_detect=True,
        builder=_FakeBuilder(),
        user_id="alice",
    )

    assert len(recorded.audits) == 1, recorded.audits
    assert recorded.audits[0]["action"] == "document_upload"
    assert recorded.audits[0]["user_id"] == "alice"
    assert recorded.audits[0]["tenant_id"] == "admin"
