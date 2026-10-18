"""Formula recognition helpers — response cleanup and LaTeX confidence scoring.

``formula_recognizer`` (188 lines) had no tests. It runs on every detected
formula region during ingestion (``builder.py`` calls ``recognize()``), and its
output goes straight into the document index, so a parser that mangles the
model's response quietly corrupts indexed content.

These tests cover the two pure methods:

  * ``_extract_latex`` — pull the formula out of whatever wrapper the model used
    (code fence, ``$...$``, ``$$...$$``, bare);
  * ``_validate_latex`` — a heuristic confidence used to decide whether the
    recognition is trustworthy enough to keep.

The validate tests intentionally pin the *current* scoring shape, including one
known weakness (bracket pairing ignores bracket *type*), so a future fix has a
test to flip rather than a silent behaviour change.
"""

import pytest

from core.ingestion.layout.formula_recognizer import FormulaRecognizer


@pytest.fixture
def rec():
    return FormulaRecognizer()


class TestExtractLatex:
    def test_fenced_block_is_unwrapped(self, rec):
        assert rec._extract_latex("```latex\nE=mc^2\n```") == "E=mc^2"

    def test_fence_without_language_tag(self, rec):
        assert rec._extract_latex("```\nx^2\n```") == "x^2"

    def test_fence_wins_over_surrounding_prose(self, rec):
        text = "Here is the formula:\n```latex\na^2+b^2=c^2\n```\nHope that helps."
        assert rec._extract_latex(text) == "a^2+b^2=c^2"

    def test_display_math_delimiters_stripped(self, rec):
        assert rec._extract_latex("$$\\frac{a}{b}$$") == "\\frac{a}{b}"

    def test_inline_math_delimiters_stripped(self, rec):
        assert rec._extract_latex("$x=1$") == "x=1"

    def test_bare_latex_passes_through(self, rec):
        assert rec._extract_latex("\\sum_{i=1}^{n} i") == "\\sum_{i=1}^{n} i"

    def test_surrounding_whitespace_trimmed(self, rec):
        assert rec._extract_latex("   x = 1   \n") == "x = 1"

    def test_multiline_fence_is_kept_whole(self, rec):
        out = rec._extract_latex("```latex\n\\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}\n```")
        assert "\\begin{aligned}" in out
        assert "\\end{aligned}" in out

    def test_empty_input(self, rec):
        assert rec._extract_latex("") == ""


class TestValidateLatex:
    def test_empty_scores_zero(self, rec):
        assert rec._validate_latex("") == 0.0

    def test_score_is_bounded(self, rec):
        for s in ("x", "E=mc^2", "\\frac{a}{b}", "((((((((("):
            score = rec._validate_latex(s)
            assert 0.0 <= score <= 1.0, f"{s!r} scored {score}"

    def test_balanced_formula_with_commands_scores_high(self, rec):
        # braces balanced (+0.2), math command (+0.15), equals (+0.1),
        # script (+0.05), on a 0.5 base
        assert rec._validate_latex("\\frac{a}{b} = c^2") >= 0.9

    def test_unbalanced_braces_lose_score(self, rec):
        good = rec._validate_latex("\\frac{a}{b} = c")
        bad = rec._validate_latex("\\frac{a}{b = c")
        assert bad < good

    def test_unclosed_bracket_penalty_scales_with_depth(self, rec):
        one = rec._validate_latex("x{")
        two = rec._validate_latex("x{{")
        assert two <= one, "deeper imbalance must not score higher"

    def test_stray_closing_bracket_is_penalised(self, rec):
        assert rec._validate_latex("}x") < rec._validate_latex("x")

    def test_math_command_raises_score(self, rec):
        assert rec._validate_latex("\\sum") > rec._validate_latex("sum")

    def test_equals_raises_score(self, rec):
        assert rec._validate_latex("a=b") > rec._validate_latex("ab")

    def test_scripts_raise_score(self, rec):
        assert rec._validate_latex("x^2") > rec._validate_latex("x2")

    def test_plain_prose_scores_low(self, rec):
        """KNOWN WEAKNESS (documented, not endorsed).

        Scoring gives 0.5 base + 0.2 for "no unbalanced brackets" = 0.7 to any
        bracket-free string, including a sentence the model emitted instead of a
        formula. 0.7 is the same score a bare symbol like ``x`` gets, so the
        confidence cannot separate "a formula" from "prose about a formula".

        Fixing this means changing the scoring shape (e.g. requiring a math
        command or an operator for the bracket bonus), which changes what gets
        kept at ingestion — out of scope for a test-only pass. Pinned here so
        the gap is visible and a future fix flips this assertion.
        """
        prose = rec._validate_latex("I cannot see any formula in this image")
        real = rec._validate_latex("\\frac{a}{b} = c^2")
        assert prose == pytest.approx(0.7), "scoring shape changed — re-evaluate"
        assert real > prose, "a real formula must still outscore prose"

    def test_mismatched_bracket_types_are_not_detected(self, rec):
        """KNOWN WEAKNESS (documented, not endorsed).

        The validator matches on stack depth only, so ``{]`` counts as balanced
        and earns the +0.2 bonus. This test pins the current behaviour so that
        when type-aware matching is implemented the assertion flips — and the
        gap is visible in the suite instead of living in a comment.
        """
        matched = rec._validate_latex("{]")
        balanced = rec._validate_latex("{}")
        assert matched >= balanced - 0.01, (
            "behaviour changed: brackets are now type-checked (good — flip "
            "this test to assert matched < balanced)"
        )
