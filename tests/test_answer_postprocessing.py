"""Answer cleanup must remove artifacts without deleting legitimate content."""
from core.retrieval.synthesizer import AnswerSynthesizer


def _clean(text: str) -> str:
    synth = object.__new__(AnswerSynthesizer)
    return synth._post_process_answer(text)


def test_release_notes_answer_is_not_truncated():
    version = "根据检索内容，Release Notes 的版本号为 2.5.0。"
    author = "根据检索内容，Release Notes 的作者是 secmes。"
    assert _clean(version) == version
    assert _clean(author) == author


def test_regular_explanatory_vocabulary_is_preserved():
    text = "The text states the dimensions.\nNote: values are rounded."
    assert _clean(text) == text


def test_standalone_artifact_lines_are_removed():
    text = "Self-Correction: revise the draft\nFinal answer: 42.\nDrafting: hidden"
    assert _clean(text) == "Final answer: 42."
