"""Grid reconstruction helpers — the geometry layer of the pinout-table path.

``grid_reconstructor`` turns a ruled grid (pinout diagrams: rows/columns labelled
on the edges) into a markdown table. It had no tests at all, yet it sits on the
ingestion path for every PDF page (``parser.py`` calls it behind
``GRID_RECONSTRUCTION_ENABLED``).

The interesting behaviour is all in the pure helpers, so those are tested
directly: position merging, label reassembly, band assignment, cell text
clustering — plus the rejection thresholds that keep an ordinary ruled financial
table from being mistaken for a labelled grid.
"""

from core.ingestion.layout.grid_reconstructor import (
    _MAX_COL_BAND,
    _MAX_ROW_BAND,
    _MIN_BAND,
    _MIN_GRID_COLS,
    _MIN_GRID_ROWS,
    _MIN_NON_EMPTY_CELLS,
    _cell_text,
    _label_bands,
    _merge_label_chars,
    _merge_positions,
)


class TestMergePositions:
    def test_near_duplicates_collapse_to_one_line(self):
        # Two ruled lines 0.8pt apart are one visual line.
        assert len(_merge_positions([10.0, 10.8])) == 1

    def test_distant_positions_stay_separate(self):
        assert _merge_positions([10.0, 20.0]) == [10.0, 20.0]

    def test_collapsed_value_is_the_mean(self):
        merged = _merge_positions([10.0, 11.0])
        assert merged == [10.5]

    def test_chain_of_close_positions_collapses_progressively(self):
        # 10.0 -> 11.0 -> 12.0 are each within tolerance of the running mean,
        # so a wall of closely spaced rules becomes one line rather than three.
        merged = _merge_positions([10.0, 11.0, 12.0])
        assert len(merged) == 1

    def test_unsorted_input_is_sorted(self):
        assert _merge_positions([30.0, 10.0, 20.0]) == [10.0, 20.0, 30.0]

    def test_empty(self):
        assert _merge_positions([]) == []

    def test_tolerance_is_configurable(self):
        assert len(_merge_positions([10.0, 14.0], tol=10.0)) == 1
        assert len(_merge_positions([10.0, 14.0], tol=1.0)) == 2


class TestMergeLabelChars:
    @staticmethod
    def word(text, x0, x1, top, bottom):
        return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}

    def test_adjacent_chars_on_one_baseline_join(self):
        words = [self.word("A", 10, 16, 100, 110), self.word("F", 16.5, 22, 100, 110)]
        merged = _merge_label_chars(words)
        assert len(merged) == 1
        assert merged[0]["text"] == "AF"

    def test_digits_join_the_same_way(self):
        words = [self.word("2", 10, 16, 100, 110), self.word("3", 17, 23, 100, 110)]
        assert _merge_label_chars(words)[0]["text"] == "23"

    def test_different_lines_do_not_join(self):
        words = [self.word("A", 10, 16, 100, 110), self.word("B", 16.5, 22, 130, 140)]
        assert len(_merge_label_chars(words)) == 2

    def test_far_apart_words_do_not_join(self):
        # Same baseline but a wide gap = two separate labels, not one.
        words = [self.word("A", 10, 16, 100, 110), self.word("B", 40, 46, 100, 110)]
        assert len(_merge_label_chars(words)) == 2

    def test_merged_bbox_covers_the_run(self):
        words = [self.word("A", 10, 16, 100, 110), self.word("F", 16.5, 22, 100, 112)]
        m = _merge_label_chars(words)[0]
        assert m["x1"] == 22
        assert m["bottom"] == 112

    def test_input_words_are_not_mutated(self):
        w = self.word("A", 10, 16, 100, 110)
        _merge_label_chars([w, self.word("F", 16.5, 22, 100, 110)])
        assert w["text"] == "A", "helper must copy, not mutate the caller's dict"

    def test_empty(self):
        assert _merge_label_chars([]) == []


class TestLabelBands:
    def test_label_assigns_to_the_band_it_sits_in(self):
        bands = [(0.0, 10.0), (10.0, 20.0)]
        labels = [("A", 5.0), ("B", 15.0)]
        assert _label_bands(bands, labels) == [("A", 0.0, 10.0), ("B", 10.0, 20.0)]

    def test_unlabelled_bands_are_dropped(self):
        bands = [(0.0, 10.0), (10.0, 20.0)]
        labels = [("A", 5.0)]
        assert len(_label_bands(bands, labels)) == 1

    def test_adjacent_bands_sharing_a_label_merge(self):
        # A label spanning two bands (a tall merged cell) yields one entry.
        bands = [(0.0, 10.0), (10.0, 20.0)]
        labels = [("A", 5.0), ("A", 15.0)]
        out = _label_bands(bands, labels)
        assert len(out) == 1
        assert out[0] == ("A", 0.0, 20.0), "spanning label must cover both bands"

    def test_label_just_outside_the_band_still_counts(self):
        # Tolerance of 2pt absorbs label/line alignment error.
        bands = [(10.0, 20.0)]
        assert _label_bands(bands, [("A", 8.5)]) == [("A", 10.0, 20.0)]

    def test_label_far_outside_does_not_count(self):
        bands = [(10.0, 20.0)]
        assert _label_bands(bands, [("A", 2.0)]) == []

    def test_empty(self):
        assert _label_bands([], []) == []


class TestCellText:
    def test_chars_on_one_line_join_in_x_order(self):
        items = [(100.0, 20.0, "C"), (100.0, 10.0, "A"), (100.0, 15.0, "B")]
        assert _cell_text(items) == "ABC"

    def test_lines_join_top_to_bottom(self):
        items = [(100.0, 10.0, "A"), (120.0, 10.0, "B")]
        assert _cell_text(items) == "AB"

    def test_wide_vertical_gap_starts_a_new_line(self):
        # Beyond _CELL_LINE_GAP the characters are different visual lines; they
        # still concatenate, but in top-to-bottom order.
        items = [(100.0, 10.0, "Z"), (160.0, 10.0, "A")]
        assert _cell_text(items) == "ZA"

    def test_empty(self):
        assert _cell_text([]) == ""


class TestRejectionThresholds:
    """These constants decide whether a page is treated as a labelled grid.

    Too permissive and every ruled financial table becomes a pinout diagram;
    too strict and real diagrams are missed. They are asserted here so a
    tuning change is a visible decision rather than a silent one.
    """

    def test_minimum_rows_and_cols_are_sane(self):
        assert _MIN_GRID_ROWS >= 3, "a 2-row grid is not a grid"
        assert _MIN_GRID_COLS >= 3

    def test_minimum_non_empty_cells_is_reachable(self):
        assert _MIN_NON_EMPTY_CELLS <= _MIN_GRID_ROWS * _MIN_GRID_COLS, (
            "the non-empty threshold must be satisfiable by a minimum-size grid"
        )

    def test_band_geometry_bounds_are_ordered(self):
        """A band narrower than _MIN_BAND is noise; wider than the max is not a
        single row/column. The ordering of these bounds is the invariant."""
        assert _MIN_BAND > 0
        assert _MAX_ROW_BAND > _MIN_BAND
        assert _MAX_COL_BAND > _MIN_BAND
