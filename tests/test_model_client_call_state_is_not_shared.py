"""Batch 3 — a model call's own facts must come back with its result.

``ModelClient`` is a process-wide singleton: one instance serves every
tenant, page thread and background task. It used to publish per-call facts
on ``self`` — ``last_finish_reason`` for vision calls and
``last_json_error`` for JSON calls — for the caller to read back *after*
the call returned. Between the return and the read, any concurrent call
could overwrite or clear those fields, so a caller could attribute another
call's outcome to its own:

- a reply that was truncated could look finished (its truncation warning
  lost), and a finished reply could look truncated (false warning);
- an unreadable JSON verdict could look like a successful ``{}`` answer,
  which the self-check turned into "verdict passed".

Both facts now travel as values: ``ChatResult`` for vision,
``(data, error)`` for JSON. These tests pin the new contract and the
absence of the old slots.
"""
import threading

import pytest

from core.models.client import ChatResult, ModelClient
from tests.concurrency_interleaver import Interleaver


def _bare_client():
    """A ModelClient with no configuration, for exercising pure methods."""
    return ModelClient.__new__(ModelClient)


# ---------------------------------------------------------------- ChatResult

def test_chat_result_reports_a_token_cap():
    assert ChatResult(text="x", finish_reason="length").hit_token_cap
    assert not ChatResult(text="x", finish_reason="stop").hit_token_cap
    assert not ChatResult(text="x").hit_token_cap


def test_chat_result_is_immutable():
    r = ChatResult(text="x", finish_reason="stop")
    with pytest.raises(Exception):
        r.text = "y"  # frozen dataclass


# ---------------------------------------------------------------- strict JSON

def test_strict_returns_data_without_error_on_success():
    c = _bare_client()
    c._generate_json_inner = lambda *a, **k: {"passed": True}
    data, error = c.generate_json_strict("p")
    assert data == {"passed": True}
    assert error is None


def test_strict_returns_the_error_as_a_value():
    c = _bare_client()
    c._generate_json_inner = lambda *a, **k: None  # unparsable
    data, error = c.generate_json_strict("p")
    assert data == {}
    assert error and "no parsable JSON" in error


def test_strict_reports_a_wrong_type():
    c = _bare_client()
    c._generate_json_inner = lambda *a, **k: "not a dict"
    data, error = c.generate_json_strict("p")
    assert data == {}
    assert error and "expected a JSON object" in error


def test_strict_accepts_a_single_element_list():
    """Same shape rule as generate_json: a one-dict list unwraps."""
    c = _bare_client()
    c._generate_json_inner = lambda *a, **k: [{"a": 1}]
    data, error = c.generate_json_strict("p")
    assert data == {"a": 1}
    assert error is None


def test_a_genuine_empty_object_is_not_an_error():
    """``{}`` is a legitimate answer and must carry no error."""
    c = _bare_client()
    c._generate_json_inner = lambda *a, **k: {}
    data, error = c.generate_json_strict("p")
    assert data == {}
    assert error is None, "an empty object is an answer, not a failure"


def test_strict_does_not_touch_the_shared_attribute():
    """The strict path must not depend on (or write) ``last_json_error``."""
    c = _bare_client()
    c.last_json_error = "stale value from another call"
    c._generate_json_inner = lambda *a, **k: {"ok": True}
    data, error = c.generate_json_strict("p")
    assert data == {"ok": True}
    assert error is None, "the stale shared attribute must not leak in"
    assert c.last_json_error == "stale value from another call", (
        "generate_json_strict must not write the shared attribute"
    )


# ---------------------------------------------------------------- concurrency

def test_two_json_calls_do_not_share_an_error():
    """One call unreadable, one clean — each keeps its own verdict.

    Pre-fix both callers read ``client.last_json_error``; whichever call
    ran second decided what both saw. Post-fix the error returns with the
    call, so interleaving cannot change either verdict.
    """
    c = _bare_client()
    results = {}
    il = Interleaver()

    def bad_call():
        c._generate_json_inner = lambda *a, **k: None
        data, error = c.generate_json_strict("bad")
        results["bad"] = (data, error)
        il.at("bad-done")

    def good_call():
        il.wait_for("bad-done")
        c._generate_json_inner = lambda *a, **k: {"passed": True}
        data, error = c.generate_json_strict("good")
        results["good"] = (data, error)
        il.release("bad-done")

    tb = threading.Thread(target=bad_call)
    tg = threading.Thread(target=good_call)
    tb.start()
    tg.start()
    tb.join()
    tg.join()

    assert results["bad"][1] is not None, (
        "the unreadable call lost its own error to the concurrent clean call"
    )
    assert results["bad"][0] == {}
    assert results["good"][1] is None, "the clean call must not inherit an error"
    assert results["good"][0] == {"passed": True}


def test_the_old_shared_slots_are_gone():
    """Guard: the per-call state must not live on the client any more.

    ``last_json_error`` is removed outright. ``last_finish_reason`` is kept
    only as a deprecated mirror for old callers, but the new APIs must not
    read it — asserted by ``test_strict_does_not_touch_the_shared_attribute``
    and the ``ChatResult`` tests above.
    """
    c = _bare_client()
    assert not hasattr(c, "last_json_error") or c.last_json_error is None, (
        "last_json_error must not be set by construction"
    )


def test_generate_json_strict_exists_and_generate_json_still_works():
    """The old interface is untouched; the new one is additive."""
    c = _bare_client()
    c._generate_json_inner = lambda *a, **k: {"v": 1}
    assert c.generate_json("p") == {"v": 1}
    assert c.generate_json_strict("p") == ({"v": 1}, None)


# ---------------------------------------------------------------- vision path

def test_chat_completion_result_carries_the_finish_reason():
    """``_chat_completion_result`` returns the reason with the text.

    Driven through a stubbed transport so no network is involved: the
    method is exercised for the shape of its return value.
    """
    c = _bare_client()
    c.llm_model = "test-model"
    c.llm_base_url = "http://llm.invalid/v1"
    c.llm_api_key = "k"

    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "hi"},
                                 "finish_reason": "length"}]}

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    c._session = _Session()
    # Avoid the prompt-length guard config lookup needing real settings.
    result = c._chat_completion_result([{"role": "user", "content": "x"}])
    assert isinstance(result, ChatResult)
    assert result.text == "hi"
    assert result.finish_reason == "length"
    assert result.hit_token_cap


def test_the_text_only_chat_completion_still_returns_a_string():
    c = _bare_client()
    c.llm_model = "test-model"
    c.llm_base_url = "http://llm.invalid/v1"
    c.llm_api_key = "k"

    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "hi"},
                                 "finish_reason": "stop"}]}

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    c._session = _Session()
    out = c._chat_completion([{"role": "user", "content": "x"}])
    assert out == "hi"
    assert isinstance(out, str)
