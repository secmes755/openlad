"""An unreadable JSON reply must be distinguishable from an empty one.

``generate_json`` returns ``{}`` both when the model genuinely answered ``{}`` and
when its reply could not be read as a JSON object. The failure was logged, but no
caller could react to it — a planner whose reply was unreadable looked exactly
like a planner that found no candidate. The failure is now also reported through
``client.last_json_error``, mirroring the existing ``last_finish_reason``
convention for truncated replies.
"""
from core.models.client import ModelClient


def _client(monkeypatch, reply: str) -> ModelClient:
    """A client whose generate() returns `reply` without touching the network."""
    client = ModelClient.__new__(ModelClient)
    client.llm_base_url = "http://llm.invalid/v1"
    client.llm_api_key = "k"
    client.llm_model = "test-model"
    client.last_json_error = None
    monkeypatch.setattr(client, "generate", lambda *args, **kwargs: reply)
    return client


def test_parseable_object_is_returned_without_an_error(monkeypatch):
    client = _client(monkeypatch, '{"category": "power", "confidence": 0.9}')
    assert client.generate_json("prompt") == {"category": "power", "confidence": 0.9}
    assert client.last_json_error is None


def test_unparseable_reply_records_a_reason(monkeypatch):
    client = _client(monkeypatch, "I am afraid I cannot answer that in JSON.")
    assert client.generate_json("prompt") == {}
    assert client.last_json_error, "callers cannot tell this from an empty object"


def test_empty_answer_and_empty_object_are_distinguishable(monkeypatch):
    answered_empty = _client(monkeypatch, "{}")
    assert answered_empty.generate_json("p") == {}
    assert answered_empty.last_json_error is None, "the model really did answer {}"

    nothing_at_all = _client(monkeypatch, "")
    assert nothing_at_all.generate_json("p") == {}
    assert nothing_at_all.last_json_error is not None, "nothing came back at all"


def test_a_later_success_clears_the_reason(monkeypatch):
    client = _client(monkeypatch, "not json at all")
    client.generate_json("p")
    assert client.last_json_error is not None

    monkeypatch.setattr(client, "generate", lambda *args, **kwargs: '{"ok": true}')
    assert client.generate_json("p") == {"ok": True}
    assert client.last_json_error is None
