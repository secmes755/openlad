"""An unreadable self-check verdict must not be recorded as a pass.

``generate_json`` returns ``{}`` both when the reviewer genuinely answered
``{}`` and when its reply could not be parsed; ``last_json_error`` is what tells
them apart. The self-check read only ``result.get("passed", True)``, so an
unreadable reply took the default, the answer was passed through unchanged, and
the unchanged comparison marked the check as *passed* — pushing confidence to
``high`` on a verdict that was never obtained.

The correct outcome is ``None`` ("not verified"), not ``False``: we do not know
the answer is wrong, and rewriting it on no evidence is worse than leaving it.
"""
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


def test_unparseable_verdict_is_not_a_pass():
    synth, client = _synth_with_reply("I am afraid I cannot answer that in JSON.")
    answer, checked = synth._self_check("营业收入多少？", ANSWER, "ctx")

    assert client.last_json_error, "precondition: the reply was unreadable"
    assert answer == ANSWER, "an unreadable verdict must not rewrite the answer"
    assert checked is False, "an unreadable verdict is not a pass"


def test_unparseable_verdict_yields_none_confidence_signal():
    """The caller's mapping: checked=False must become self_check_passed=None.

    Reproduces synthesizer.py:156-164 without a live model: the gate is on, the
    verdict is unreadable, so the answer is unchanged but the check did not
    happen.
    """
    synth, client = _synth_with_reply("not json")

    answer_before_check = ANSWER
    answer, checked = synth._self_check("q", ANSWER, "ctx", industry_pack=object())
    self_check_passed = (answer == answer_before_check) if checked else None

    assert client.last_json_error, "precondition: the verdict was unreadable"
    assert answer == answer_before_check, "answer untouched"
    assert self_check_passed is None, "unchanged must not be reported as a pass"


def test_genuine_pass_is_still_recorded_as_passed():
    synth, client = _synth_with_reply('{"passed": true}')
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert client.last_json_error is None
    assert answer == ANSWER
    assert checked is True


def test_a_failing_verdict_with_a_correction_still_corrects():
    synth, _ = _synth_with_reply('{"passed": false, "corrected_answer": "营业收入为 99 亿元。"}')
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == "营业收入为 99 亿元。"
    assert checked is True


def test_a_failing_verdict_without_a_correction_keeps_the_answer():
    """"passed": false with nothing to substitute is a completed check."""
    synth, _ = _synth_with_reply('{"passed": false}')
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == ANSWER
    assert checked is True


def test_exception_during_the_check_is_not_a_pass(monkeypatch):
    synth, _ = _synth_with_reply("{}")
    monkeypatch.setattr(synth.model_client, "generate_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    answer, checked = synth._self_check("q", ANSWER, "ctx")
    assert answer == ANSWER
    assert checked is False


def test_only_true_may_reach_high_confidence():
    """_assess_confidence must not treat the None signal as a pass."""
    synth = AnswerSynthesizer.__new__(AnswerSynthesizer)
    text = ANSWER * 20
    ctx = ANSWER * 40
    assert synth._assess_confidence(text, ctx, None) != "high"
    assert synth._assess_confidence(text, ctx, False) != "high"
