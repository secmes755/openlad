"""An unreadable self-check verdict must not be recorded as a pass.

``generate_json`` returns ``{}`` both when the reviewer genuinely answered
``{}`` and when its reply could not be parsed. The self-check read only
``result.get("passed", True)``, so an unreadable reply took the default, the
answer was passed through unchanged, and the unchanged comparison marked the
check as *passed* — pushing confidence to ``high`` on a verdict that was never
obtained.

The first fix read the reason off ``client.last_json_error``. That attribute
lives on the process-wide model client, so a concurrent call could clear or
overwrite it between the return and the check, and the unreadable reply was
again taken for a pass. The reason now comes back as a value
(``generate_json_strict`` → ``(data, error)``), so it belongs to this call.

The correct outcome is ``None`` ("not verified"), not ``False``: we do not know
the answer is wrong, and rewriting it on no evidence is worse than leaving it.
"""
import threading

from core.models.client import ModelClient
from core.retrieval.synthesizer import AnswerSynthesizer

ANSWER = "营业收入为 100 亿元。"


def _synth_with_reply(reply: str) -> tuple[AnswerSynthesizer, ModelClient]:
    synth = AnswerSynthesizer.__new__(AnswerSynthesizer)
    client = ModelClient.__new__(ModelClient)
    client.llm_base_url = "http://llm.invalid/v1"
    client.llm_api_key = "k"
    client.llm_model = "test-model"
    client.last_json_error = None
    client.last_finish_reason = None
    client.generate = lambda *a, **k: reply
    synth.model_client = client
    synth._extract_relevant_context = lambda *a, **k: ANSWER
    return synth, client


def _strict_from_reply(reply: str):
    """A ``generate_json_strict`` double that mirrors the real parsing.

    The real method parses the model reply; here the reply is already a
    string, so an unparsable one is any value that is not valid JSON.
    """
    import json

    def _strict(*a, **k):
        try:
            data = json.loads(reply)
        except Exception:
            return {}, "no parsable JSON in the model reply"
        if not isinstance(data, dict):
            return {}, f"expected a JSON object, got {type(data).__name__}"
        return data, None

    return _strict


def test_unparseable_verdict_is_not_a_pass():
    synth, _ = _synth_with_reply("I am afraid I cannot answer that in JSON.")
    synth.model_client.generate_json_strict = _strict_from_reply(
        "I am afraid I cannot answer that in JSON.")
    answer, checked = synth._self_check("营业收入多少？", ANSWER, "ctx")

    assert answer == ANSWER, "an unreadable verdict must not rewrite the answer"
    assert checked is False, "an unreadable verdict is not a pass"


def test_unparseable_verdict_yields_none_confidence_signal():
    """The caller's mapping: checked=False must become self_check_passed=None.

    Reproduces synthesizer.py:156-164 without a live model: the gate is on, the
    verdict is unreadable, so the answer is unchanged but the check did not
    happen.
    """
    synth, _ = _synth_with_reply("not json")
    synth.model_client.generate_json_strict = _strict_from_reply("not json")

    answer_before_check = ANSWER
    answer, checked = synth._self_check("q", ANSWER, "ctx", industry_pack=object())
    self_check_passed = (answer == answer_before_check) if checked else None

    assert answer == answer_before_check, "answer untouched"
    assert self_check_passed is None, "unchanged must not be reported as a pass"


def test_the_error_comes_back_as_a_value_not_a_shared_attribute():
    """The reason must not depend on ``client.last_json_error``.

    A concurrent call clears that attribute on the shared singleton; the
    verdict must still be judged unreadable because its own call returned the
    error. Here the attribute is left clear while the strict call reports an
    error — the old code would have called this a pass.
    """
    synth, client = _synth_with_reply("garbage")
    client.last_json_error = None  # as if another call had just cleared it
    client.generate_json_strict = _strict_from_reply("garbage")

    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == ANSWER
    assert checked is False, (
        "the verdict was judged by a shared attribute that another call "
        "could clear; it must be judged by the call's own returned error"
    )


def test_genuine_pass_is_still_recorded_as_passed():
    synth, _ = _synth_with_reply('{"passed": true}')
    # The default generate_json_strict in the base client parses via the real
    # path; here we drive the strict double so no live model is needed.
    synth.model_client.generate_json_strict = _strict_from_reply('{"passed": true}')
    synth.model_client._generate_json_inner = lambda *a, **k: {"passed": True}
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == ANSWER
    assert checked is True


def test_a_failing_verdict_with_a_correction_still_corrects():
    synth, _ = _synth_with_reply('{"passed": false, "corrected_answer": "营业收入为 99 亿元。"}')
    synth.model_client.generate_json_strict = _strict_from_reply(
        '{"passed": false, "corrected_answer": "营业收入为 99 亿元。"}')
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == "营业收入为 99 亿元。"
    assert checked is True


def test_a_failing_verdict_without_a_correction_keeps_the_answer():
    """"passed": false with nothing to substitute is a completed check."""
    synth, _ = _synth_with_reply('{"passed": false}')
    synth.model_client.generate_json_strict = _strict_from_reply('{"passed": false}')
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == ANSWER
    assert checked is True


def test_exception_during_the_check_is_not_a_pass(monkeypatch):
    synth, _ = _synth_with_reply("{}")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(synth.model_client, "generate_json_strict", _boom)
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == ANSWER
    assert checked is False


def test_a_concurrent_call_cannot_flip_an_unreadable_verdict_into_a_pass():
    """End-to-end shape of the race: two checks, one unreadable.

    Pre-fix the unreadable check read ``last_json_error`` after the other
    check's call had cleared it, so it saw "no error" and treated the
    unreadable verdict as a pass. The error now returns with the call, so the
    interleaving cannot change the outcome.
    """
    synth_a, _ = _synth_with_reply("garbage")
    synth_a.model_client.generate_json_strict = _strict_from_reply("garbage")

    # Another synthesizer sharing the same client, whose call succeeds and
    # (pre-fix) would have cleared the shared error flag.
    synth_b, _ = _synth_with_reply('{"passed": true}')
    synth_b.model_client = synth_a.model_client

    order = []

    def run_a():
        ans, checked = synth_a._self_check("q", ANSWER, "ctx")
        order.append(("a", checked))

    def run_b():
        # A "successful" concurrent call in the window between A's call
        # returning and A's verdict read. With the error as a value this is
        # irrelevant; with a shared attribute it flipped A to a pass.
        synth_a.model_client.last_json_error = None

    ta = threading.Thread(target=run_a)
    tb = threading.Thread(target=run_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert order == [("a", False)], (
        "an unreadable verdict was flipped to a pass by a concurrent call "
        "clearing the shared error attribute"
    )


def test_only_true_may_reach_high_confidence():
    """_assess_confidence must not treat the None signal as a pass."""
    synth = AnswerSynthesizer.__new__(AnswerSynthesizer)
    text = ANSWER * 20
    ctx = ANSWER * 40
    assert synth._assess_confidence(text, ctx, None) != "high"
    assert synth._assess_confidence(text, ctx, False) != "high"
