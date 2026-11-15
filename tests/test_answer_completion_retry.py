"""One bounded repair for visibly incomplete short generations."""
from types import SimpleNamespace

from core.retrieval.router import IntentType
from core.retrieval.synthesizer import AnswerSynthesizer


class _Registry:
    def resolve_plugin_for_categories(self, _categories):
        return None

    def detect_plugin_for_text(self, _text):
        return None

    def compose_with_base(self, plugin):
        return plugin


class _Model:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def prompt_char_limit(_max_tokens):
        return 5000

    def generate(self, _prompt, **_kwargs):
        self.calls += 1
        return "根据检索内容，Release" if self.calls == 1 else "作者：secmes"


def test_incomplete_short_answer_detection_is_bounded():
    check = AnswerSynthesizer._needs_completion_retry
    assert check("根据检索内容，Release")
    assert not check("作者：secmes。")
    assert not check("RK3568")
    assert not check("x" * 100)


def test_incomplete_short_answer_gets_one_repair_attempt():
    synth = object.__new__(AnswerSynthesizer)
    synth.model_client = _Model()
    synth.plugin_registry = _Registry()
    plan = SimpleNamespace(intent=IntentType.EXACT_LOOKUP, requires_comparison=False)
    context = "Release Notes\nAuthor: secmes\nVersion: 2.5.0\n" * 4
    result = synth.synthesize("作者是谁", plan, context, [])
    assert "secmes" in result["answer"]
    assert synth.model_client.calls == 2
