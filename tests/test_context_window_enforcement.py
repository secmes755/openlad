"""Prompt/context budgets must include output and real prompt overhead."""
from core.config import settings
from core.models.client import ModelClient
from core.retrieval.retriever import SegmentMerger
from core.retrieval.truncation import MARKER


def test_model_prompt_limit_reserves_the_requested_output_window():
    cfg = settings.CONTEXT_CONFIG
    output_tokens = 4096
    expected = int((cfg["llm_max_tokens"] - output_tokens)
                   * cfg["token_to_char_ratio"]) - cfg["prompt_protocol_reserve_chars"]
    assert ModelClient.prompt_char_limit(output_tokens) == expected


def test_synthesis_budget_includes_prompt_overhead_and_output():
    cfg = settings.CONTEXT_CONFIG
    assert (cfg["synthesis_context_budget"]
            + cfg["synthesis_prompt_overhead_chars"]
            <= cfg["synthesis_safe_chars"])
    assert (cfg["synthesis_safe_chars"] + cfg["prompt_protocol_reserve_chars"]
            + int(cfg["synthesis_max_tokens"] * cfg["token_to_char_ratio"])
            <= int(cfg["llm_max_tokens"] * cfg["token_to_char_ratio"]))


def test_truncate_to_fit_never_exceeds_the_budget():
    from core.retrieval.truncation import truncate_to_fit
    out = truncate_to_fit("x" * 500, 100)
    assert len(out) <= 100
    assert out.endswith(MARKER)


def test_smart_truncate_preserves_a_keyword_line_in_newline_separated_text():
    lines = [f"unrelated register row {i:02d} " + "x" * 24 for i in range(30)]
    lines.insert(17, "GPU: Mali-G52 1-Core-2EE")
    page = "\n".join(lines)
    retriever = object.__new__(SegmentMerger)
    out = retriever._smart_truncate(page, "GPU型号", 420)
    assert "Mali-G52 1-Core-2EE" in out
    assert len(out) <= 420
    assert out.endswith(MARKER)


def test_synthesizer_fits_evidence_to_the_actual_prompt_budget():
    from types import SimpleNamespace

    from core.retrieval.router import IntentType
    from core.retrieval.synthesizer import AnswerSynthesizer

    class Registry:
        def resolve_plugin_for_categories(self, _categories):
            return None

        def detect_plugin_for_text(self, _text):
            return None

        def compose_with_base(self, plugin):
            return plugin

    class Model:
        def __init__(self):
            self.prompt = ""
            self.system = ""

        @staticmethod
        def prompt_char_limit(_max_tokens):
            return 1000

        def generate(self, prompt, system_prompt, **_kwargs):
            self.prompt = prompt
            self.system = system_prompt
            return "answer"

    synth = object.__new__(AnswerSynthesizer)
    synth.model_client = Model()
    synth.plugin_registry = Registry()
    plan = SimpleNamespace(intent=IntentType.EXACT_LOOKUP, requires_comparison=False)
    synth.synthesize("question", plan, "evidence line\n" * 200, [])
    assert len(synth.model_client.prompt) + len(synth.model_client.system) <= 1000
    assert MARKER in synth.model_client.prompt
