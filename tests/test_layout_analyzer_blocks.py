"""Layout analysis helpers — block splitting and OCR/text merging.

``layout_analyzer`` (456 lines) had no test references. Most of it needs a real
page or scipy at runtime, but the two helpers that decide *what the analyser
sees* are pure functions and are tested here.

The behaviour that matters:

  * blank lines separate blocks (a block is a paragraph, not a page);
  * bboxes are estimated monotonically down the page, so a block's bottom never
    precedes its top;
  * OCR results win over extracted text, but extracted text fills gaps — a page
    where OCR covered only half the lines must still surface the other half,
    otherwise content silently disappears from the index.
"""

import pytest

from core.ingestion.layout.layout_analyzer import LayoutAnalyzer


@pytest.fixture
def analyzer():
    # config=None is the supported default path.
    return LayoutAnalyzer()


class TestSplitTextBlocks:
    def test_blank_line_separates_blocks(self, analyzer):
        blocks = analyzer._split_text_blocks("alpha\nbeta\n\ngamma")
        assert len(blocks) == 2
        assert blocks[0][0] == "alpha\nbeta"
        assert blocks[1][0] == "gamma"

    def test_consecutive_blank_lines_do_not_create_empty_blocks(self, analyzer):
        blocks = analyzer._split_text_blocks("alpha\n\n\n\nbeta")
        assert len(blocks) == 2
        assert all(b[0].strip() for b in blocks), "no empty block may be emitted"

    def test_leading_blank_lines_are_ignored(self, analyzer):
        blocks = analyzer._split_text_blocks("\n\n\nalpha")
        assert len(blocks) == 1
        assert blocks[0][0] == "alpha"

    def test_trailing_text_without_blank_line_is_kept(self, analyzer):
        # The final block ends at EOF, not at a blank line — it must not be lost.
        blocks = analyzer._split_text_blocks("alpha\n\nbeta")
        assert [b[0] for b in blocks] == ["alpha", "beta"]

    def test_line_whitespace_is_stripped_inside_blocks(self, analyzer):
        blocks = analyzer._split_text_blocks("  alpha  \n  beta  ")
        assert blocks[0][0] == "alpha\nbeta"

    def test_empty_input(self, analyzer):
        assert analyzer._split_text_blocks("") == []

    def test_whitespace_only_input(self, analyzer):
        assert analyzer._split_text_blocks("\n\n   \n\n") == []

    def test_bbox_vertical_order_is_monotonic(self, analyzer):
        blocks = analyzer._split_text_blocks("one\n\ntwo\n\nthree")
        tops = [b[1][1] for b in blocks]
        bottoms = [b[1][3] for b in blocks]
        assert tops == sorted(tops), "block top must increase down the page"
        assert bottoms == sorted(bottoms), "block bottom must increase down the page"

    def test_bbox_is_not_inverted(self, analyzer):
        for _, bbox in analyzer._split_text_blocks("a\nb\n\nc"):
            assert bbox[1] <= bbox[3], f"inverted bbox {bbox} (top after bottom)"

    def test_bbox_width_follows_the_longest_line(self, analyzer):
        _, short = analyzer._split_text_blocks("a\n\nbbbbbbbbbb")[0]
        long_blocks = analyzer._split_text_blocks("aaaaaaaaaa\n\nb")
        _, long = long_blocks[0]
        assert long[2] > short[2]

    def test_bbox_starts_at_x_zero(self, analyzer):
        for _, bbox in analyzer._split_text_blocks("a\n\nb"):
            assert bbox[0] == 0


class TestMergeOcrBlocks:
    @staticmethod
    def ocr(text, bbox=(0, 0, 10, 10), conf=0.9):
        return {"text": text, "bbox": list(bbox), "confidence": conf}

    def test_ocr_results_become_blocks(self, analyzer):
        blocks = analyzer._merge_ocr_blocks([], [self.ocr("hello")])
        assert blocks[0][0] == "hello"
        assert blocks[0][2] == 0.9

    def test_blank_ocr_text_is_skipped(self, analyzer):
        blocks = analyzer._merge_ocr_blocks([], [self.ocr("   ")])
        assert blocks == []

    def test_extracted_text_fills_gaps_left_by_ocr(self, analyzer):
        """The critical case: OCR covered one line, extraction had two."""
        text_blocks = [("line one from pdf", [0, 0, 100, 20]),
                       ("line two from pdf", [0, 20, 100, 40])]
        blocks = analyzer._merge_ocr_blocks(
            text_blocks, [self.ocr("line one from pdf")])
        texts = [b[0] for b in blocks]
        assert "line one from pdf" in texts
        assert "line two from pdf" in texts, (
            "a line missing from OCR must still be indexed from extraction"
        )

    def test_ocr_wins_for_the_same_text(self, analyzer):
        text_blocks = [("same text here", [0, 0, 100, 20])]
        blocks = analyzer._merge_ocr_blocks(
            text_blocks, [self.ocr("same text here", conf=0.99)])
        matching = [b for b in blocks if b[0] == "same text here"]
        assert len(matching) == 1, "duplicate for the same text"
        assert matching[0][2] == 0.99, "OCR confidence must win over the 0.5 estimate"

    def test_short_extracted_fragments_are_not_added(self, analyzer):
        # Fragments of <=10 chars are layout noise (page numbers, bullets).
        text_blocks = [("ab", [0, 0, 10, 10]), ("a real sentence here", [0, 10, 200, 30])]
        blocks = analyzer._merge_ocr_blocks(text_blocks, [])
        texts = [b[0] for b in blocks]
        assert "ab" not in texts
        assert "a real sentence here" in texts

    def test_backfilled_blocks_get_mid_confidence(self, analyzer):
        blocks = analyzer._merge_ocr_blocks(
            [("extracted text long enough", [0, 0, 100, 20])], [])
        assert blocks[0][2] == 0.5

    def test_empty_inputs(self, analyzer):
        assert analyzer._merge_ocr_blocks([], []) == []


class TestEstimateBbox:
    def test_every_block_gets_mid_confidence(self, analyzer):
        out = analyzer._estimate_bbox([("a", [0, 0, 10, 10]), ("b", [0, 10, 10, 20])])
        assert [b[2] for b in out] == [0.5, 0.5]

    def test_text_and_bbox_are_preserved(self, analyzer):
        out = analyzer._estimate_bbox([("text", [1, 2, 3, 4])])
        assert out[0][0] == "text"
        assert out[0][1] == [1, 2, 3, 4]
