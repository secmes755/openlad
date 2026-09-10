"""LLM client: exactly one Session, and no retry of a deterministic rejection.

Two defects in core/models/client.py:

1. the ``session`` property created a Session without holding ``self._lock`` (the
   lock existed but was never used anywhere), so concurrent first use — page
   analysis runs in a thread pool — could build several Sessions and leak every
   loser's connection pool for the process lifetime;
2. ``_chat_completion`` retried every failure three times, including 4xx
   rejections where the identical request cannot possibly succeed; the embedding
   path already classified 4xx as "rejected" and returned instead of retrying.

The 300 s read timeout is deliberately not touched: that is a product decision
about how long one attempt may block, not part of this defect.
"""
import threading
import time

import pytest

from core.models import client as client_mod
from core.models.client import ModelClient


def _bare_client(session=None) -> ModelClient:
    """Build a client without config/DB resolution (attribute-level construction)."""
    c = ModelClient.__new__(ModelClient)
    c.llm_base_url = "http://llm.invalid/v1"
    c.llm_api_key = "k"
    c.llm_model = "test-model"
    c.ocr_base_url = ""
    c._session = session
    c._lock = threading.Lock()
    c.last_finish_reason = None
    return c


class _FakeResponse:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise client_mod.requests.HTTPError(
                f"{self.status_code} error", response=self)


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self._response


def test_session_is_created_once_under_concurrent_first_use(monkeypatch):
    created = []

    class SlowSession:
        def __init__(self):
            created.append(self)
            time.sleep(0.05)          # widen the window between check and assign

    monkeypatch.setattr(client_mod.requests, "Session", SlowSession)
    client = _bare_client()

    seen, barrier = [], threading.Barrier(8, timeout=10)

    def grab():
        barrier.wait()
        seen.append(client.session)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(created) == 1, f"{len(created)} Sessions created (connection pools leaked)"
    assert len({id(s) for s in seen}) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_rejected_status_is_not_retried(monkeypatch, status):
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    fake = _FakeSession(_FakeResponse(status, text="rejected payload"))
    client = _bare_client(session=fake)

    assert client._chat_completion([{"role": "user", "content": "hi"}]) == ""
    assert fake.calls == 1, f"HTTP {status} was retried {fake.calls} times"


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_status_is_still_retried(monkeypatch, status):
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    fake = _FakeSession(_FakeResponse(status, text="try again later"))
    client = _bare_client(session=fake)

    assert client._chat_completion([{"role": "user", "content": "hi"}]) == ""
    assert fake.calls == 3, f"HTTP {status} should still be retried, got {fake.calls} calls"
