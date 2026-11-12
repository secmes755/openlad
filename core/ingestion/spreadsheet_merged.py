"""Restore values only within explicitly merged Excel cell regions."""
import math
from pathlib import Path

MergedRegion = tuple[int, int, int, int, object]
# zero-based inclusive row/col bounds, plus the source anchor for type fidelity


def read_merged_regions(path: str | Path, sheet_name: str) -> list[MergedRegion]:
    """Read actual merge declarations without interpreting spreadsheet content."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == '.xlsx':
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise RuntimeError('openpyxl is required for .xlsx merge metadata') from exc

        workbook = load_workbook(source, read_only=False, data_only=True)
        try:
            sheet = workbook[sheet_name]
            return [(
                cell_range.min_row - 1, cell_range.min_col - 1,
                cell_range.max_row - 1, cell_range.max_col - 1,
                sheet.cell(cell_range.min_row, cell_range.min_col).value,
            ) for cell_range in sheet.merged_cells.ranges]
        finally:
            workbook.close()
    if suffix == '.xls':
        try:
            import xlrd
        except ImportError as exc:
            raise RuntimeError('xlrd is required for .xls merge metadata') from exc

        workbook = xlrd.open_workbook(
            str(source), formatting_info=True, on_demand=True,
        )
        try:
            sheet = workbook.sheet_by_name(sheet_name)
            regions = []
            for row_low, row_high, col_low, col_high in sheet.merged_cells:
                anchor_cell = sheet.cell(row_low, col_low)
                anchor = (bool(anchor_cell.value)
                          if anchor_cell.ctype == xlrd.XL_CELL_BOOLEAN
                          else anchor_cell.value)
                regions.append((
                    row_low, col_low, row_high - 1, col_high - 1, anchor,
                ))
            return regions
        finally:
            workbook.release_resources()
    return []


def _empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return math.isnan(value)
    return False


def restore_merged_cells(frame, regions: list[MergedRegion]) -> int:
    """Fill empty subordinate cells from a non-empty merge anchor.

    Existing values are never overwritten, and no value is propagated outside
    the declared rectangle. Returns the number of cells actually filled.
    """
    filled = 0
    for row_low, col_low, row_high, col_high, source_anchor in regions:
        if min(row_low, col_low, row_high, col_high) < 0:
            raise ValueError('Merged region bounds must be non-negative')
        if row_high < row_low or col_high < col_low:
            raise ValueError('Invalid merged region bounds')
        # pandas may drop trailing all-empty rows/columns before merge metadata
        # is applied. Recreate only the declared rectangle's missing axes.
        for col in range(len(frame.columns), col_high + 1):
            frame[col] = ''
        for row in range(len(frame.index), row_high + 1):
            frame.loc[row, :] = ''
        if row_low >= len(frame.index) or col_low >= len(frame.columns):
            continue
        frame_anchor = frame.iat[row_low, col_low]
        # pandas' Excel engines coerce booleans to 0/1; retain the source type.
        if isinstance(source_anchor, bool):
            frame.iat[row_low, col_low] = source_anchor
        anchor = source_anchor if isinstance(source_anchor, bool) else frame_anchor
        if _empty(anchor):
            continue
        for row in range(row_low, min(row_high + 1, len(frame.index))):
            for col in range(col_low, min(col_high + 1, len(frame.columns))):
                if row == row_low and col == col_low:
                    continue
                if _empty(frame.iat[row, col]):
                    frame.iat[row, col] = anchor
                    filled += 1
    return filled
