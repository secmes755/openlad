"""Two hot-path costs in query execution that the request did not need to pay.

Neither is a wrong answer, so the assertions are about the work done — calls made,
query plans chosen — rather than about returned values.

* ``_resolve_doc_filter`` lists up to ``doc_filter_list_limit`` (10k) documents and
  is reached once per step in two separate passes over the same plan (the quota
  pass and the step pass), so a request with repeated filters paid for the full
  listing several times.
* The planner loads documents by ingestion state on every query
  (``status IN ('verified','degraded') ORDER BY created_at DESC``) and ``documents``
  had no index on ``status``, so each of those was a full table scan.
"""
from types import SimpleNamespace

from core.db.tenant_db import TenantMetadataDB
from core.retrieval.executor import RetrievalExecutor

DOCS = [
    {"id": "doc-1", "title": "AB1234 Datasheet", "filename": "ab1234.pdf"},
    {"id": "doc-2", "title": "AB5678 Datasheet", "filename": "ab5678.pdf"},
]


class _CountingDocs:
    """Stands in for the metadata DB and counts how often documents are listed."""

    def __init__(self, docs):
        self._docs = docs
        self.list_calls = 0

    def list_documents(self, limit=None):
        self.list_calls += 1
        return list(self._docs)


def _executor(monkeypatch) -> tuple[RetrievalExecutor, _CountingDocs]:
    ex = RetrievalExecutor(tenant_id="admin")
    docs = _CountingDocs(DOCS)
    monkeypatch.setattr(ex, "metadata_db", docs)
    monkeypatch.setattr(ex, "merger", SimpleNamespace(merge=lambda *a, **k: ("", [])))
    monkeypatch.setattr(ex, "_execute_step", lambda *a, **k: [])
    monkeypatch.setattr(ex, "max_chars", 100_000)
    return ex, docs


def _two_step_plan():
    return {
        "strategy": "compare_docs",
        "steps": [
            {"tool": "single_retrieve", "query": "q1", "doc_filter": ["AB1234"]},
            {"tool": "single_retrieve", "query": "q2", "doc_filter": ["AB1234"]},
        ],
    }


def test_one_request_lists_documents_once_despite_a_repeated_filter(monkeypatch):
    ex, docs = _executor(monkeypatch)

    ex.execute(_two_step_plan())

    assert docs.list_calls == 1, (
        "the same filter was resolved more than once inside one request "
        f"({docs.list_calls} document listings)"
    )


def test_a_new_request_does_not_reuse_the_previous_snapshot(monkeypatch):
    """The memo must not outlive the request it belongs to.

    The executor is cached per tenant and reused, so anything longer-lived would
    still be serving the document list from before an upload.
    """
    ex, docs = _executor(monkeypatch)
    plan = {"strategy": "single_retrieve",
            "steps": [{"tool": "single_retrieve", "query": "q", "doc_filter": ["AB1234"]}]}

    ex.execute(plan)
    ex.execute(plan)

    assert docs.list_calls == 2


def test_resolving_outside_a_request_keeps_the_previous_behaviour(monkeypatch):
    """Callers that bypass execute() (tests, ad-hoc use) get no caching at all."""
    ex, docs = _executor(monkeypatch)

    assert ex._resolve_doc_filter(["AB1234"]) == ["doc-1"]
    assert ex._resolve_doc_filter(["AB1234"]) == ["doc-1"]

    assert docs.list_calls == 2


def test_the_planner_document_query_is_served_by_the_status_index(tmp_path):
    db = TenantMetadataDB(tmp_path / "metadata.db")

    with db.get_connection() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM documents "
            "WHERE status IN ('verified', 'degraded') ORDER BY created_at DESC"
        ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan).upper()

    assert "IDX_DOCUMENTS_STATUS" in detail, f"status index not used: {detail}"
    assert "SCAN DOCUMENTS" not in detail, f"full scan of documents: {detail}"
