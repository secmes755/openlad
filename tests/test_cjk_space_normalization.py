"""Unit tests for ingestion-time spaced-CJK normalization.

pdfplumber sometimes emits one character per text run ("瑞 芯 微" for
"瑞芯微"). The trigram FTS tokenizer cannot match a normal query term
against such text, so page text is normalized before storage. The rule is
deliberately narrow: only runs of 3+ single CJK chars separated by spaces
are folded, per line, so table cells like "是 否" are preserved.
"""
import unittest

from core.ingestion.parser import normalize_spaced_cjk


class TestSpacedCjkNormalization(unittest.TestCase):
    def test_long_spaced_run_folds(self):
        self.assertEqual(
            normalize_spaced_cjk("瑞 芯 微 新 产 品 发 布 总 结"),
            "瑞芯微新产品发布总结",
        )

    def test_three_chars_fold(self):
        self.assertEqual(normalize_spaced_cjk("瑞 芯 微"), "瑞芯微")

    def test_two_chars_not_folded(self):
        # Two adjacent single chars are likely separate table cells.
        self.assertEqual(normalize_spaced_cjk("是 否"), "是 否")

    def test_fullwidth_space_folds(self):
        self.assertEqual(normalize_spaced_cjk("瑞　芯　微"), "瑞芯微")

    def test_mixed_ascii_and_fullwidth_spaces_fold(self):
        self.assertEqual(normalize_spaced_cjk("瑞 芯　微"), "瑞芯微")

    def test_no_folding_across_lines(self):
        self.assertEqual(
            normalize_spaced_cjk("瑞 芯 微\n芯 微"),
            "瑞芯微\n芯 微",
        )

    def test_surrounding_text_preserved(self):
        # A spaced run folds greedily until the spacing stops: "瑞 芯 微 发"
        # is one extractor artefact, so the trailing "发" joins the run.
        self.assertEqual(
            normalize_spaced_cjk("标题：瑞 芯 微 发布"),
            "标题：瑞芯微发布",
        )
        # A spaced run followed by an unspaced char still folds greedily:
        # the extractor emitted one char per run, so the whole sequence is
        # one phrase. (Matches the offline FTS acceptance gate exactly.)
        self.assertEqual(
            normalize_spaced_cjk("RK 瑞 芯 微 芯片"),
            "RK 瑞芯微芯片",
        )

    def test_markdown_table_row_untouched(self):
        row = "| 是 | 否 | 备注 |"
        self.assertEqual(normalize_spaced_cjk(row), row)

    def test_non_cjk_spacing_untouched(self):
        # Only CJK runs are folded; digits and ASCII tokens keep their
        # spacing by design (the offline FTS gate measured CJK-only folding).
        self.assertEqual(normalize_spaced_cjk("2 0 2 5"), "2 0 2 5")
        self.assertEqual(normalize_spaced_cjk("N P U"), "N P U")

    def test_clean_text_unchanged(self):
        for text in ("瑞芯微新产品发布总结", "2025/07/19", "",
                     "Normal English sentence with spaces.",
                     "营业收入 280 亿元"):
            self.assertEqual(normalize_spaced_cjk(text), text)

    def test_idempotent(self):
        once = normalize_spaced_cjk("瑞 芯 微 新 产 品")
        self.assertEqual(normalize_spaced_cjk(once), once)


if __name__ == "__main__":
    unittest.main()
