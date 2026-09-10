"""A truncated context must not be reported as high confidence.

The confidence heuristic looks for a truncation marker in the synthesized
context. Two defects made that signal useless:

1. case mismatch — the retrieval side emitted lowercase ``...[truncated]`` while
   the check looked for uppercase ``[TRUNCATED``, so the marker never registered
   and a cut-short context still scored ``high``;
2. silent cuts — three call sites (executor standard and decomposed, engine
   deep-research) truncated the context with no marker at all, so no string check
   could see them even in principle.

Producers now append the canonical marker from ``core.retrieval.truncation`` and
the detector reads it through the same module.
"""
from pathlib import Path

import pytest

from core.retrieval.synthesizer import AnswerSynthesizer
from core.retrieval.truncation import MARKER, is_truncated, mark_truncated

ANSWER = (
    "VDD_NPU_S0 is supplied by RK806_BUCK2 at 0.75 V, with a 22 uF decoupling "
    "capacitor and a maximum current of 3 A."
)
COMPLETE_CONTEXT = "===== Sub-query 1 =====\n" + ("power tree content " * 40)

LEGACY_MARKERS = [
    "body\n...[truncated]",
    "body\n...[content truncated]",
    "body\n... (context truncated)",
    "body\n\n[TRUNCATED — context size exceeded]",
    "body\n\n[CONTENT TRUNCATED BY SAFETY GUARD]",
]


def test_canonical_marker_is_detected():
    assert is_truncated(mark_truncated("body")) is True
    assert is_truncated(f"body\n{MARKER}") is True


@pytest.mark.parametrize("text", LEGACY_MARKERS)
def test_markers_the_old_check_missed_are_detected(text):
    assert is_truncated(text) is True


def test_complete_context_is_not_flagged():
    assert is_truncated(COMPLETE_CONTEXT) is False
    assert is_truncated("") is False


def test_truncation_removes_the_high_confidence_verdict():
    synth = AnswerSynthesizer()
    assert synth._assess_confidence(
        ANSWER, COMPLETE_CONTEXT, self_check_passed=True) == "high"

    # The historical failure mode: a retrieval-side lowercase marker.
    legacy_truncated = COMPLETE_CONTEXT[:200] + "\n...[truncated]"
    assert synth._assess_confidence(
        ANSWER, legacy_truncated, self_check_passed=True) != "high"

    # And the canonical marker now written by every producer.
    assert synth._assess_confidence(
        ANSWER, mark_truncated(COMPLETE_CONTEXT), self_check_passed=True) != "high"


def test_producers_do_not_append_ad_hoc_markers():
    """Every truncation must go through the module, not hand-written prose."""
    pkg = Path(__file__).resolve().parent.parent / "core" / "retrieval"
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        if path.name == "truncation.py":      # names the legacy markers on purpose
            continue
        src = path.read_text(encoding="utf-8")
        for bad in ("...[truncated]", "...[content truncated]", "(context truncated)"):
            if bad in src:
                offenders.append(f"{path.name}: {bad}")
    assert not offenders, f"legacy truncation markers still written: {offenders}"
