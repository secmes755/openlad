"""Request-scoped state must not live on the tenant-cached executor.

Regression test for a cross-request contamination bug.

`RetrievalExecutor` instances are cached per tenant by
``QueryEngine._get_components`` and shared by concurrent requests, so anything
stored on ``self`` is visible to every other in-flight request for that tenant.
``industry_hint`` is supplied by the client on each request
(``core/api/routes/query.py`` passes ``req.industry``); it used to be assigned
to ``self.industry_hint`` at the top of ``execute()`` and read back further down
the same call chain. Two concurrent requests for one tenant with different
hints could therefore apply each other's industry pack rules (query expansion,
chapter boost rules, spec-fact terms).

The test forces the two requests to overlap inside ``execute()`` and asserts
each one's hint arrives at the merger unchanged.
"""

import threading

from core.retrieval.executor import RetrievalExecutor


def _plan() -> dict:
    return {
        "strategy": "single_retrieve",
        "steps": [
            {
                "tool": "single_retrieve",
                "query": "clock frequency",
                "doc_filter": [],
                "purpose": "lookup",
            }
        ],
    }


def test_concurrent_requests_keep_their_own_industry_hint(monkeypatch):
    executor = RetrievalExecutor(tenant_id=None)

    barrier = threading.Barrier(2, timeout=10)
    observed: list[str] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def fake_execute_step(tool, query, doc_filter, purpose, max_context_quota=None,
                          original_query=None, industry_hint=None):
        # Both requests are now inside execute() at the same time, past the point
        # where the hint used to be written onto the shared instance.
        barrier.wait()
        return []

    def fake_merge(results, max_context_chars=None, query="", industry_hint=None):
        with lock:
            observed.append(industry_hint)
        return "", []

    monkeypatch.setattr(executor, "_execute_step", fake_execute_step)
    monkeypatch.setattr(executor.merger, "merge", fake_merge)

    def run(hint: str) -> None:
        try:
            executor.execute(_plan(), tenant_id=None, industry_hint=hint,
                             original_query="q")
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    hints = ("financial", "semiconductor")
    threads = [threading.Thread(target=run, args=(h,)) for h in hints]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, f"execute() raised: {errors}"
    assert sorted(observed) == sorted(hints), (
        "each request must carry its own industry_hint through the call chain; "
        f"expected {sorted(hints)}, observed {observed}"
    )
