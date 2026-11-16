"""Merged-cell restoration must attribute a label, not replicate prose.

pandas is optional in the minimal-dependency image, so this module skips
wholesale there.
"""
import pytest

pd = pytest.importorskip("pandas")
from core.ingestion.spreadsheet_merged import (  # noqa: E402
    MERGE_ANCHOR_MAX_CHARS,
    restore_merged_cells,
)

# --- a long anchor is attributed, not replicated ---------------------------

def test_long_merge_anchor_is_not_replicated():
    frame = pd.DataFrame(index=range(30), columns=range(12), dtype=object)
    anchor = "本文件所含信息为商业机密，未经书面许可不得复制、传播。" * 3
    frame.iat[0, 0] = anchor
    filled = restore_merged_cells(frame, [(0, 0, 29, 11, anchor)])
    assert filled == 0
    assert frame.iat[0, 0] == anchor


def test_short_merge_anchor_still_fills_its_region():
    frame = pd.DataFrame(index=range(4), columns=range(2), dtype=object)
    frame.iat[0, 0] = "Port B"
    filled = restore_merged_cells(frame, [(0, 0, 3, 1, "Port B")])
    assert filled == 7


def test_anchor_length_limit_is_the_documented_value():
    assert MERGE_ANCHOR_MAX_CHARS > 0


