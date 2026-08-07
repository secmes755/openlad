"""Ruled-grid reconstructor — rebuilds labeled grid diagrams as clean tables.

Problem: text extraction flattens 2D grid diagrams (BGA ball maps, register
maps, coordinate tables drawn as figures) into scrambled 1D text. The garbled
output pairs values incorrectly and actively misleads downstream LLM reasoning.

Insight: the grid geometry (ruling lines) and every character's position are
preserved in the PDF, so the table can be rebuilt deterministically from
vector information alone — no model calls, no guessing.

Generic: works for any page region with drawn ruling lines plus edge labels
(alphabetic row labels at the left/right margin, numeric column labels at the
top/bottom margin). Returns None when a page contains no such grid, so it is
safe to run on every page during ingestion.
"""
import logging
import re
from collections import defaultdict

logger = logging.getLogger(__name__)

# Band geometry bounds (points). Generous ranges covering common grid diagrams.
_MIN_BAND = 3.0
_MAX_ROW_BAND = 40.0
_MAX_COL_BAND = 60.0
_LINE_MERGE_TOL = 2.0
# Line clustering inside a cell: a new visual line starts beyond this gap.
_CELL_LINE_GAP = 2.0
# Minimum labeled rows/cols to accept a grid (avoids false positives on
# ordinary ruled tables, which lack alphabetic edge labels).
_MIN_GRID_ROWS = 4
_MIN_GRID_COLS = 4
# Minimum non-empty cells for a credible grid.
_MIN_NON_EMPTY_CELLS = 8


def _merge_positions(values: list[float], tol: float = _LINE_MERGE_TOL) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
        else:
            out[-1] = (out[-1] + v) / 2
    return out


def _merge_label_chars(words: list[dict]) -> list[dict]:
    """Merge horizontally adjacent same-baseline words into multi-char labels.

    Text extraction often splits edge labels like 'AF' or '23' into single
    characters; join them back in reading order.
    """
    words = sorted(words, key=lambda w: (round((w['top'] + w['bottom']) / 2), w['x0']))
    merged: list[dict] = []
    for w in words:
        if merged:
            p = merged[-1]
            same_line = abs((w['top'] + w['bottom']) / 2 - (p['top'] + p['bottom']) / 2) < 4
            gap = w['x0'] - p['x1']
            if same_line and -1 < gap < 4:
                p['text'] += w['text']
                p['x1'] = max(p['x1'], w['x1'])
                p['bottom'] = max(p['bottom'], w['bottom'])
                continue
        merged.append(dict(w))
    return merged


def _label_bands(bands: list[tuple[float, float]],
                 labels: list[tuple[str, float]]) -> list[tuple[str, float, float]]:
    """Assign edge labels to bands; merge adjacent bands sharing one label."""
    labeled: list[tuple[str, float, float]] = []
    for lo, hi in bands:
        lab = next((t for t, c in labels if lo - 2 <= c <= hi + 2), None)
        if lab is None:
            continue
        if labeled and labeled[-1][0] == lab:
            prev = labeled[-1]
            labeled[-1] = (lab, prev[1], hi)
        else:
            labeled.append((lab, lo, hi))
    return labeled


def _cell_text(items: list[tuple[float, float, str]]) -> str:
    """Assemble cell text from positioned chars via tight line clustering."""
    items = sorted(items, key=lambda x: (x[0], x[1]))
    lines: list[list] = []
    for top, x0, ch in items:
        if lines and top - lines[-1][0] <= _CELL_LINE_GAP:
            lines[-1][1].append((x0, ch))
        else:
            lines.append([top, [(x0, ch)]])
    return ''.join(''.join(c for _, c in sorted(ln[1])) for ln in lines)


def reconstruct_grid_table(plumber_page) -> str | None:
    """Rebuild the labeled ruled grid on a pdfplumber page as a markdown table.

    Returns a markdown table with columns ``Coordinate | Label`` (only
    non-empty cells), or None when the page has no qualifying grid.
    """
    try:
        page_w = plumber_page.width
        h_raw, v_raw = [], []
        for ln in plumber_page.lines:
            if abs(ln['top'] - ln['bottom']) < 0.5:
                h_raw.append(ln['top'])
            elif abs(ln['x0'] - ln['x1']) < 0.5:
                v_raw.append(ln['x0'])
        for r in plumber_page.rects:
            h_raw.extend((r['top'], r['bottom']))
            v_raw.extend((r['x0'], r['x1']))
        hs = _merge_positions(h_raw)
        vs = _merge_positions(v_raw)
        if len(hs) < _MIN_GRID_ROWS + 1 or len(vs) < _MIN_GRID_COLS + 1:
            return None

        words = plumber_page.extract_words(x_tolerance=2, y_tolerance=2)

        # Row labels: single letters at the left/right page margin.
        row_cands = [w for w in words
                     if re.match(r'^[A-Z]$', w['text'])
                     and (w['x1'] < page_w * 0.15 or w['x0'] > page_w * 0.85)]
        row_labels = [(w['text'], (w['top'] + w['bottom']) / 2)
                      for w in _merge_label_chars(row_cands)]
        if len({t for t, _ in row_labels}) < _MIN_GRID_ROWS:
            return None

        row_bands = [(hs[i], hs[i + 1]) for i in range(len(hs) - 1)
                     if _MIN_BAND < hs[i + 1] - hs[i] < _MAX_ROW_BAND]
        rows = _label_bands(row_bands, row_labels)
        if len(rows) < _MIN_GRID_ROWS:
            return None

        # Column labels: single digits just above the first or below the last row.
        grid_top, grid_bottom = rows[0][1], rows[-1][2]
        col_cands = [w for w in words
                     if re.match(r'^\d$', w['text'])
                     and (grid_top - 25 <= (w['top'] + w['bottom']) / 2 <= grid_top
                          or grid_bottom <= (w['top'] + w['bottom']) / 2 <= grid_bottom + 25)]
        col_labels = [(w['text'], (w['x0'] + w['x1']) / 2)
                      for w in _merge_label_chars(col_cands)]
        col_bands = [(vs[i], vs[i + 1]) for i in range(len(vs) - 1)
                     if _MIN_BAND < vs[i + 1] - vs[i] < _MAX_COL_BAND]
        cols = _label_bands(col_bands, col_labels)
        if len(cols) < _MIN_GRID_COLS:
            return None

        # Assign every char inside the grid to its cell.
        cells: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
        for ch in plumber_page.chars:
            cy = (ch['top'] + ch['bottom']) / 2
            cx = (ch['x0'] + ch['x1']) / 2
            if cy < grid_top or cy > grid_bottom:
                continue
            row = next((rw for rw in rows if rw[1] <= cy <= rw[2]), None)
            col = next((c for c in cols if c[1] <= cx <= c[2]), None)
            if row and col:
                cells[(row[0], col[0])].append((ch['top'], ch['x0'], ch['text']))

        entries = []
        for rlab, _, _ in rows:
            for clab, _, _ in cols:
                text = _cell_text(cells.get((rlab, clab), []))
                if text:
                    entries.append((rlab, clab, text))
        if len(entries) < _MIN_NON_EMPTY_CELLS:
            return None

        lines = ['| Coordinate | Label |', '|---|---|']
        lines += [f'| {r}{c} | {t} |' for r, c, t in entries]
        logger.info(f'[GRID] Reconstructed labeled grid: {len(rows)}x{len(cols)} cells, '
                    f'{len(entries)} non-empty')
        return '\n'.join(lines)
    except Exception as e:
        logger.debug(f'[GRID] Grid reconstruction skipped: {e}')
        return None
