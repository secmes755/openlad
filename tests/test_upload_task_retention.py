"""Upload-task retention and timestamp encoding.

Three defects, one root cause: the timestamp encoding was never pinned down.

1. ``update_upload_task`` stored ``updated_at`` as an epoch float while new rows
   got the TEXT column default (``CURRENT_TIMESTAMP``). SQLite orders every REAL
   below every TEXT, so ``updated_at < datetime('now', '-N hours')`` was true for
   freshly updated rows — cleanup deleted just-completed upload tasks, and the
   task history the table exists to preserve was wiped on every upload.
2. Python 3.12 deprecated sqlite3's implicit datetime adapter and 3.13 removes
   it, so passing a ``datetime`` object raised a DeprecationWarning (and would
   raise an error on upgrade).
3. Microsecond precision produced a third shape, breaking TEXT ordering against
   ``CURRENT_TIMESTAMP``.

Tests run against a real SystemDB on a temp path — no mocks, since the bug lives
in the interaction between Python, sqlite3 and SQLite's type ordering.
"""
import sqlite3
import time
import warnings
from datetime import datetime

from core.db.system_db import SystemDB
from core.tenant.models import UserInfo


def _db(tmp_path) -> SystemDB:
    return SystemDB(tmp_path / "system.db")


def test_completed_task_survives_cleanup(tmp_path):
    """A task that just finished must not be reaped by the next upload's cleanup."""
    db = _db(tmp_path)
    db.create_upload_task("t1", "doc-1", "report.pdf", tenant_id="admin")
    db.update_upload_task("t1", status="completed", progress=100)

    assert db.cleanup_upload_tasks(max_age_hours=24) == 0
    task = db.get_upload_task("t1")
    assert task is not None, "just-completed upload task was deleted as if it were old"
    assert task["status"] == "completed"


def test_genuinely_old_task_is_still_reclaimed(tmp_path):
    """The cleanup must keep working for rows that really are older than the cutoff."""
    db = _db(tmp_path)
    db.create_upload_task("old", "doc-1", "report.pdf", tenant_id="admin")
    db.update_upload_task("old", status="completed")
    with db.get_connection() as conn:
        conn.execute("UPDATE upload_tasks SET updated_at = '2020-01-01 00:00:00' "
                     "WHERE task_id = ?", ("old",))
        conn.commit()

    assert db.cleanup_upload_tasks(max_age_hours=24) == 1
    assert db.get_upload_task("old") is None


def test_legacy_epoch_rows_are_normalised_on_open(tmp_path):
    """Rows written by the old float writer are converted, not left to be reaped."""
    path = tmp_path / "system.db"
    db = _db(tmp_path)

    # Simulate a row left behind by the previous implementation.
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO upload_tasks (task_id, doc_id, tenant_id, filename, "
            "status, progress, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "doc-1", "admin", "report.pdf", "completed", 100, time.time()),
        )
        conn.commit()
        assert conn.execute("SELECT typeof(updated_at) FROM upload_tasks "
                            "WHERE task_id = 'legacy'").fetchone()[0] == "real"

    reopened = SystemDB(path)  # init_schema runs the normalisation

    with reopened.get_connection() as conn:
        kind = conn.execute("SELECT typeof(updated_at) FROM upload_tasks "
                            "WHERE task_id = 'legacy'").fetchone()[0]
    assert kind == "text", "epoch-float timestamp was not normalised"
    assert reopened.cleanup_upload_tasks(max_age_hours=24) == 0
    assert reopened.get_upload_task("legacy") is not None


def test_datetime_writes_use_the_current_timestamp_shape(tmp_path):
    """Bound datetimes must be stored in the exact TEXT shape the default uses."""
    db = _db(tmp_path)
    user = UserInfo(
        id="u1", tenant_id="admin", username="u1", email=None,
        role="admin", api_key="k1",
        created_at=datetime(2026, 9, 10, 8, 0, 0, 123456),
    )
    db.create_user(user, password_hash="x")

    with db.get_connection() as conn:
        row = conn.execute("SELECT created_at, typeof(created_at) AS kind "
                           "FROM users WHERE id = 'u1'").fetchone()
    assert row["kind"] == "text"
    assert row["created_at"] == "2026-09-10 08:00:00", (
        "timestamp shape drifted from CURRENT_TIMESTAMP; TEXT comparisons will break"
    )


def test_no_deprecated_default_datetime_adapter(tmp_path):
    """Writing a datetime must not rely on sqlite3's removed implicit adapter."""
    db = _db(tmp_path)
    user = UserInfo(
        id="u2", tenant_id="admin", username="u2", email=None,
        role="admin", api_key="k2", created_at=datetime.now(),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert db.create_user(user, password_hash="x") is True


def test_epoch_float_compares_below_text_which_is_why_the_bug_happened(tmp_path):
    """Characterises the SQLite rule the fix depends on (documents the mechanism)."""
    db = _db(tmp_path)
    with db.get_connection() as conn:
        below = conn.execute("SELECT ? < ?", (time.time(), "2026-09-10 00:00:00")).fetchone()[0]
    assert below == 1, "REAL is expected to order below TEXT in SQLite"
    _ = sqlite3  # kept to document that no adapter/converter is registered here
