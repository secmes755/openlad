"""PDF-level removal of rotated, page-repeated text (watermarks).

Why this exists
---------------
pdfplumber assembles a page's text by sorting every character by ``(top, x0)``
and gluing them back into lines. A rotated watermark on the text layer is
therefore interleaved *inside* body words: ``LPDDR4 and LPDDR4x Power
S<glyph>upply``. The page stays structurally valid, so nothing downstream can
tell the text is broken -- while the exact-match retrieval channel (FTS) loses
the real tokens (``2160`` no longer exists on that page) and the vector channel
is diluted by the watermark volume. Cleaning the *extracted text* afterwards is
too late: the tokens were already destroyed during assembly. The PDF itself has
to be cleaned before extraction, which also covers ``extract_tables()`` and
every page render the OCR/VLM paths consume.

How detection works
-------------------
Purely geometric, with no document-, vendor- or language-specific patterns:

* a *text group* is one ``BT ... ET`` block; its effective matrix is
  ``text matrix x current transformation matrix``, so both ``Tm`` and ``cm``
  rotations are honoured;
* a group whose effective angle is neither horizontal nor vertical (rotation is
  required, see rails) scores higher for diagonal angles and for non-black
  fill colour, and again if its own text repeats inside the block;
* groups are grouped by a coarse signature (rounded angle, coarse position,
  colour, font, size bucket); a signature present on most pages of the
  document, at a consistent angle *and* position, is a watermark.

Safety rails -- a cleaning pass must never lose body text
---------------------------------------------------------
* rotation is required, so horizontal repeated headers/footers keep going
  through the line-level sanitizer in ``builder.py`` (they do not break word
  tokens the way rotated glyphs do);
* a group larger than ``max_block_bytes`` is never considered;
* a page is left untouched if filtering it would leave it with no text-showing
  operator at all, or with less than ``min_remaining_page_bytes`` of text;
* the whole document is left untouched if removal would exceed
  ``max_document_text_loss`` of its text volume (and the caller is told, so the
  document can be marked degraded instead of silently serving broken text);
* the input file is never modified; the cleaned copy is written to a cache
  directory and the caller keeps the original path when nothing was removed.

Every failure mode returns the original path with a report explaining why, so a
broken parser can never make an ingestion worse than it was before.
"""
from __future__ import annotations

import hashlib
import logging
import math
from pathlib import Path

from ...config import settings

logger = logging.getLogger(__name__)

# Signature buckets. Coarse on purpose: the same watermark must land in the
# same bucket on every page even though the operators around it differ.
_ANGLE_BUCKET_DEG = 15
_POSITION_BUCKET = 50.0
_ANGLE_EPS = 1e-6

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Matrix helpers.  A PDF matrix ``(a, b, c, d, e, f)`` maps a row vector
# ``[x y 1]`` as ``x' = a*x + c*y + e``, ``y' = b*x + d*y + f``.
# ---------------------------------------------------------------------------
def _mat_mul(m: tuple, n: tuple) -> tuple:
    """Return the matrix that applies ``m`` first and ``n`` second."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2,
    )


def _translate(tx: float, ty: float) -> tuple:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def _matrix_angle_deg(m: tuple) -> float:
    """Rotation of the matrix's x axis, normalised to (-180, 180]."""
    deg = math.degrees(math.atan2(m[1], m[0]))
    return ((deg + 180.0) % 360.0) - 180.0


def _is_rotated(angle_deg: float, threshold_deg: float) -> bool:
    """True for text that is neither horizontal nor vertical."""
    off_axis = abs(angle_deg) % 90.0
    return threshold_deg < off_axis < (90.0 - threshold_deg)


def _numbers(operands) -> list:
    out = []
    for value in operands:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            return []
    return out


def _as_text_bytes(operand) -> bytes:
    if isinstance(operand, (bytes, bytearray)):
        return bytes(operand)
    if isinstance(operand, str):
        return operand.encode("latin-1", "ignore")
    return b""


def _text_bytes_of(operator: bytes, operands) -> bytes:
    """Raw show-text bytes of one text-showing operator (fonts stay unencoded)."""
    if operator == b"TJ":
        chunks = []
        for operand in operands:
            if isinstance(operand, (bytes, bytearray, str)):
                chunks.append(_as_text_bytes(operand))
            else:
                try:
                    for item in operand:
                        chunks.append(_as_text_bytes(item))
                except TypeError:
                    continue
        return b"".join(chunks)
    if operator in (b"Tj", b"'", b'"'):
        for operand in operands:
            data = _as_text_bytes(operand)
            if data:
                return data
    return b""


def _brightness(operator: bytes, operands) -> float | None:
    """Approximate fill brightness in [0, 1] (0 = black), or None if unknown."""
    values = _numbers(operands)
    if not values:
        return None
    if operator == b"g" and len(values) == 1:
        return max(0.0, min(1.0, values[0]))
    if operator in (b"rg", b"sc", b"scn") and len(values) == 3:
        return max(0.0, min(1.0, sum(values) / 3.0))
    if operator in (b"k", b"sc", b"scn") and len(values) == 4:
        return max(0.0, min(1.0, 1.0 - values[3]))
    if operator in (b"sc", b"scn") and len(values) == 1:
        return max(0.0, min(1.0, values[0]))
    return None


def _color_key(brightness: float | None) -> str:
    if brightness is None:
        return "unknown"
    return f"{round(brightness * 10)}"


def _signature(group: dict) -> str:
    """Coarse identity of a text group, used to correlate groups across pages.

    Only geometry is used. Deliberately *not* included:

    * font -- ``/Tf`` operands are page-local resource names; the same watermark
      is emitted as ``/C0_0`` on one page and ``/TT0`` on the next;
    * colour -- the group may never set a fill colour itself and then inherits
      whatever the surrounding stream left active, which is not page-stable;
    * size -- a watermark drawn twice inside one group doubles its byte count,
      so size is checked as a tolerance (see ``max_size_ratio``) instead.
    """
    angle = round(group["angle"] / _ANGLE_BUCKET_DEG) * _ANGLE_BUCKET_DEG
    x = round(group["x"] / _POSITION_BUCKET) * _POSITION_BUCKET
    y = round(group["y"] / _POSITION_BUCKET) * _POSITION_BUCKET
    return f"angle:{angle}|pos:{x:g},{y:g}"


# ---------------------------------------------------------------------------
# Content-stream analysis
# ---------------------------------------------------------------------------
def _extract_text_groups(operations) -> list[dict]:
    """Describe every ``BT ... ET`` group of a parsed content stream.

    Returns dicts with the group's effective angle/position, its raw text
    volume, its font and colour, plus the ``operations`` index range so a
    matched group can later be removed surgically. Non-text operators are never
    described here, so they can never be removed.
    """
    groups: list[dict] = []
    ctm = _IDENTITY
    ctm_stack: list[tuple] = []
    text_matrix = _IDENTITY
    line_matrix = _IDENTITY
    leading = 0.0
    font = "none"
    brightness: float | None = None

    current: dict | None = None

    for index, (operands, operator) in enumerate(operations):
        if operator == b"q":
            ctm_stack.append(ctm)
            continue
        if operator == b"Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
            continue
        if operator == b"cm" and len(operands) >= 6:
            values = _numbers(operands[:6])
            if values:
                ctm = _mat_mul(tuple(values), ctm)
            continue

        if operator == b"BT":
            text_matrix = _IDENTITY
            line_matrix = _IDENTITY
            current = {
                "start": index, "end": None, "angle": 0.0, "x": 0.0, "y": 0.0,
                "text_bytes": 0, "show_ops": 0, "font": font,
                "color": _color_key(brightness),
                "has_matrix": False,
            }
            continue

        if operator == b"ET":
            if current is not None:
                current["end"] = index
                groups.append(current)
                current = None
            continue

        if operator == b"Tm" and len(operands) >= 6:
            values = _numbers(operands[:6])
            if values:
                text_matrix = line_matrix = tuple(values)
            continue
        if operator in (b"Td", b"TD") and len(operands) >= 2:
            values = _numbers(operands[:2])
            if values:
                line_matrix = _mat_mul(_translate(values[0], values[1]), line_matrix)
                text_matrix = line_matrix
            if operator == b"TD" and values:
                leading = -values[1]
            continue
        if operator == b"TL" and operands:
            values = _numbers(operands[:1])
            if values:
                leading = values[0]
            continue
        if operator == b"T*":
            line_matrix = _mat_mul(_translate(0.0, -leading), line_matrix)
            text_matrix = line_matrix
            continue

        if operator == b"Tf" and operands:
            font = _as_text_bytes(operands[0]).decode("latin-1", "ignore") or "none"
            continue
        if operator in (b"g", b"rg", b"k", b"sc", b"scn"):
            value = _brightness(operator, operands)
            if value is not None:
                brightness = value
            continue

        if operator in (b"Tj", b"TJ", b"'", b'"'):
            if current is None:
                continue
            if operator in (b"'", b'"'):
                line_matrix = _mat_mul(_translate(0.0, -leading), line_matrix)
                text_matrix = line_matrix
            data = _text_bytes_of(operator, operands)
            current["text_bytes"] += len(data)
            current["show_ops"] += 1
            if not current["has_matrix"]:
                effective = _mat_mul(text_matrix, ctm)
                current["angle"] = _matrix_angle_deg(effective)
                current["x"] = effective[4]
                current["y"] = effective[5]
                current["has_matrix"] = True
            continue

    # An unclosed BT (malformed stream) is dropped: partially described groups
    # must not drive a removal decision.
    return [group for group in groups if group["end"] is not None and group["has_matrix"]]


def _parse_page_operations(page):
    """Decoded concatenated operations of a page, or None when unavailable.

    ``page.get_contents()`` already resolves both a single stream object and an
    array of streams, and decompresses them (an earlier implementation read the
    raw ``/Contents`` bytes, so compressed streams were never analysed).
    """
    try:
        contents = page.get_contents()
        if contents is None:
            return None
        operations = contents.operations
        return contents, list(operations or [])
    except Exception as exc:  # noqa: BLE001 - any parse failure -> leave page alone
        logger.debug("Content stream not parseable (%s): %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _detect_from_groups(pages_groups: list[list[dict]], config: dict) -> dict:
    """Group signatures across pages and decide which ones are watermarks."""
    total_pages = len(pages_groups)
    threshold = float(config["angle_threshold_deg"])
    buckets: dict[str, dict] = {}
    for page_idx, groups in enumerate(pages_groups):
        for group in groups:
            if group["text_bytes"] < 1:
                continue
            if group["text_bytes"] > int(config["max_block_bytes"]):
                continue
            if not _is_rotated(group["angle"], threshold):
                continue  # rotation required; horizontal repeats stay with builder.py
            signature = _signature(group)
            bucket = buckets.setdefault(signature, {
                "signature": signature, "pages": set(), "angles": [], "positions": [],
                "scores": [], "sizes": [],
            })
            bucket["pages"].add(page_idx)
            bucket["angles"].append(group["angle"])
            bucket["positions"].append((group["x"], group["y"]))
            bucket["sizes"].append(group["text_bytes"])
            bucket["scores"].append(_watermark_score(group, config))

    if not total_pages:
        return {"total_pages": 0, "candidates": [], "watermark_signatures": {}}

    min_pages = max(int(config["min_pages"]),
                    math.ceil(float(config["min_repetition_ratio"]) * total_pages))
    candidates = []
    watermark_signatures: dict[str, dict] = {}

    for signature, bucket in buckets.items():
        page_count = len(bucket["pages"])
        if page_count < min_pages:
            continue
        # A watermark may be drawn more than once inside its group (which doubles
        # its byte count); a wildly different volume means two unrelated texts
        # were lumped together, so the bucket is not a watermark after all.
        size_ratio = max(bucket["sizes"]) / max(1, min(bucket["sizes"]))
        position_consistency = _position_consistency(bucket["positions"],
                                                     float(config["position_tolerance"]))
        angle_consistency = _angle_consistency(bucket["angles"])
        avg_score = sum(bucket["scores"]) / len(bucket["scores"])
        is_watermark = size_ratio <= float(config["max_size_ratio"]) and (
            (avg_score > 0.5 and position_consistency > 0.5
             and angle_consistency > float(config["min_angle_consistency"]))
            or page_count >= total_pages * 0.9
        )
        candidate = {
            "signature": signature,
            "pages": page_count,
            "total_pages": total_pages,
            "repetition_ratio": page_count / total_pages,
            "position_consistency": round(position_consistency, 3),
            "angle_consistency": round(angle_consistency, 3),
            "score": round(avg_score, 3),
            "avg_bytes": int(sum(bucket["sizes"]) / len(bucket["sizes"])),
            "size_ratio": round(size_ratio, 2),
            "is_watermark": bool(is_watermark),
        }
        candidates.append(candidate)
        if is_watermark:
            watermark_signatures[signature] = {"page_indices": sorted(bucket["pages"]),
                                               "candidate": candidate}

    candidates.sort(key=lambda c: (c["is_watermark"], c["repetition_ratio"], c["score"]),
                    reverse=True)
    return {"total_pages": total_pages, "candidates": candidates,
            "watermark_signatures": watermark_signatures}


def _watermark_score(group: dict, config: dict) -> float:
    """Watermark likelihood in [0, 1]; rotation is the strongest signal."""
    score = 0.0
    angle = abs(group["angle"])
    off_axis = angle % 90.0
    score += 0.4
    if 15.0 <= off_axis <= 75.0:
        score += 0.2
    if 30.0 <= off_axis <= 60.0:
        score += 0.1
    if group["color"] != "0" and group["color"] != "unknown":
        score += 0.15  # non-black fill
    if group["show_ops"] > 2:
        score += 0.15  # same text repeated inside the block
    return min(score, 1.0)


def _position_consistency(positions: list[tuple], tolerance: float) -> float:
    if len(positions) < 2:
        return 0.0
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    x_range = max(xs) - min(xs)
    y_range = max(ys) - min(ys)
    if x_range <= tolerance and y_range <= tolerance:
        return 1.0
    return max(0.0, 1.0 - max(x_range, y_range) / 100.0)


def _angle_consistency(angles: list[float]) -> float:
    if len(angles) < 2:
        return 0.0
    normalized = [abs(a) % 90.0 for a in angles]
    spread = max(normalized) - min(normalized)
    if spread <= 10.0:
        return 1.0
    return max(0.0, 1.0 - spread / 90.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def sanitize_pdf_watermark(file_path: str | Path,
                           out_dir: str | Path | None = None,
                           config: dict | None = None) -> tuple[str, dict]:
    """Remove rotated page-repeated text from a PDF.

    Returns ``(path_to_use, report)``. ``path_to_use`` is the cleaned copy when
    something was removed, otherwise the original path unchanged. The report
    always explains the decision (detection candidates, rails that fired,
    errors) so callers can record it and mark documents degraded when a
    detected watermark could not be removed.
    """
    settings_config = getattr(settings, "PDF_WATERMARK_CONFIG", None) or {}
    effective = dict(settings_config)
    if config:
        effective.update(config)
    source = Path(file_path)
    report: dict = {
        "enabled": bool(effective.get("enabled", True)),
        "detected": False, "cleaned": False, "output_path": None,
        "pages_scanned": 0, "pages_modified": 0, "pages_skipped": [],
        "affected_pages": [], "signatures": [], "removed_text_bytes": 0,
        "document_text_bytes": 0, "skipped_reason": None, "error": None,
    }
    if not report["enabled"] or source.suffix.lower() != ".pdf":
        report["skipped_reason"] = "disabled" if not report["enabled"] else "not_a_pdf"
        return str(source), report

    try:
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:  # noqa: BLE001
        report["skipped_reason"] = "pypdf_unavailable"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return str(source), report

    try:
        reader = PdfReader(str(source), strict=False)
        page_groups: list[list[dict]] = []
        for page in reader.pages:
            parsed = _parse_page_operations(page)
            if parsed is None:
                page_groups.append([])
                continue
            _contents, operations = parsed
            groups = _extract_text_groups(operations)
            page_groups.append(groups)
        report["pages_scanned"] = len(reader.pages)

        detection = _detect_from_groups(page_groups, effective)
        report["signatures"] = detection["candidates"]
        watermark_signatures = detection["watermark_signatures"]
        report["detected"] = bool(watermark_signatures)

        affected: set[int] = set()
        for entry in watermark_signatures.values():
            affected.update(entry["page_indices"])
        report["affected_pages"] = sorted(page + 1 for page in affected)

        if not watermark_signatures:
            report["skipped_reason"] = "no_watermark_detected"
            return str(source), report

        document_text_bytes = sum(group["text_bytes"]
                                  for groups in page_groups for group in groups)
        report["document_text_bytes"] = document_text_bytes
        planned_removal = 0
        for page_index in affected:
            for group in page_groups[page_index]:
                if _signature(group) in watermark_signatures and _is_rotated(
                        group["angle"], float(effective["angle_threshold_deg"])):
                    planned_removal += group["text_bytes"]
        max_document_loss = float(effective["max_document_text_loss"])
        if document_text_bytes and planned_removal > document_text_bytes * max_document_loss:
            report["skipped_reason"] = "document_loss_rail"
            report["removed_text_bytes"] = planned_removal
            return str(source), report

        # Pages are rewritten through a writer, never through the reader:
        # ``PageObject.replace_contents()`` on a reader page is deprecated in
        # pypdf ("has proved being unreliable") and here it nulled the reader's
        # content objects, producing a stripped file.
        output = _output_path(source, out_dir)
        writer = PdfWriter(clone_from=str(source))
        min_remaining = int(effective["min_remaining_page_bytes"])
        threshold = float(effective["angle_threshold_deg"])
        modified = 0
        removed_bytes = 0
        for page_index in sorted(affected):
            writer_page = writer.pages[page_index]
            parsed = _parse_page_operations(writer_page)
            if parsed is None:
                report["pages_skipped"].append({"page": page_index + 1,
                                                "reason": "stream_unavailable"})
                continue
            contents, operations = parsed
            groups = _extract_text_groups(operations)
            drop: list[tuple[int, int]] = []
            dropped_bytes = 0
            for group in groups:
                if _signature(group) not in watermark_signatures:
                    continue
                if not _is_rotated(group["angle"], threshold):
                    continue
                drop.append((group["start"], group["end"]))
                dropped_bytes += group["text_bytes"]
            if not drop:
                continue

            kept: list = []
            for index, operation in enumerate(operations):
                if any(start <= index <= end for start, end in drop):
                    continue
                kept.append(operation)
            page_text_bytes = sum(g["text_bytes"] for g in groups)
            remaining_bytes = page_text_bytes - dropped_bytes
            # A page must keep a usable body: never clean away the page's whole
            # text, and never leave it with a negligible remainder.
            if not any(operator in (b"Tj", b"TJ", b"'", b'"') for _operands, operator in kept):
                report["pages_skipped"].append({"page": page_index + 1,
                                                "reason": "would_empty_page"})
                continue
            if remaining_bytes < min_remaining:
                report["pages_skipped"].append({"page": page_index + 1,
                                                "reason": "insufficient_remaining_text",
                                                "remaining_bytes": remaining_bytes})
                continue

            contents.operations = kept
            writer_page.replace_contents(contents)
            modified += 1
            removed_bytes += dropped_bytes

        if not modified:
            writer.close()
            report["skipped_reason"] = "no_page_modified"
            return str(source), report

        with open(output, "wb") as handle:
            writer.write(handle)
        writer.close()

        report["cleaned"] = True
        report["output_path"] = str(output)
        report["pages_modified"] = modified
        report["removed_text_bytes"] = removed_bytes
        logger.info(
            "PDF watermark removal: %s -> %s page(s) cleaned, %d of %d text bytes removed "
            "(%s)", source.name, modified, removed_bytes, document_text_bytes,
            [c["signature"] for c in report["signatures"] if c["is_watermark"]],
        )
        return str(output), report

    except Exception as exc:  # noqa: BLE001 - ingestion must not break here
        logger.warning("PDF watermark removal failed for %s: %s", source.name, exc)
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["skipped_reason"] = "error"
        report["cleaned"] = False
        report["output_path"] = None
        return str(source), report


def _output_path(source: Path, out_dir: str | Path | None) -> Path:
    """Content-addressed cleaned copy, so re-parsing the same upload is a no-op."""
    if out_dir is None:
        out_dir = Path(settings.DATA_DIR) / "sanitized"
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return directory / f"{digest.hexdigest()[:32]}.pdf"


def drop_rotated_characters(plumber_page):
    """Character-level fallback for pages the PDF-level pass could not clean.

    Used only for pages where a rotated, page-repeated watermark was *detected*
    but the PDF could not be rewritten (rail or parse failure). pdfplumber drops
    objects before text assembly, which keeps those glyphs out of body words.
    Callers keep the unfiltered page for ``extract_tables()``.
    """
    def _keep(obj):
        if obj.get("object_type") != "char":
            return True
        matrix = obj.get("matrix")
        if not matrix:
            return True
        # matrix = (a, b, c, d, e, f); b and c carry the rotation.
        return abs(matrix[1]) < _ANGLE_EPS and abs(matrix[2]) < _ANGLE_EPS

    try:
        return plumber_page.filter(_keep)
    except Exception as exc:  # noqa: BLE001 - a fallback must never break extraction
        logger.debug("Rotated-character filter unavailable: %s", exc)
        return plumber_page


def text_integrity_warnings(report: dict | None, max_pages_listed: int = 8) -> list[str]:
    """Warnings for the degradation signal: a detected watermark we could not fix.

    Deliberately summarised: a document may hit a rail on hundreds of pages, and
    an ingest warning has to stay readable (it surfaces in the UI and in
    retrieval's incomplete-source flag).
    """
    if not report or not report.get("detected"):
        return []
    warnings = []
    if not report.get("cleaned"):
        warnings.append(
            "rotated repeated text (watermark) detected on "
            f"{len(report.get('affected_pages') or [])} page(s) but page-level removal "
            f"was skipped ({report.get('skipped_reason') or report.get('error') or 'unknown'})"
        )
    by_reason: dict[str, list[int]] = {}
    for skipped in report.get("pages_skipped") or []:
        by_reason.setdefault(skipped["reason"], []).append(skipped["page"])
    for reason, pages in sorted(by_reason.items()):
        shown = ", ".join(str(page) for page in pages[:max_pages_listed])
        if len(pages) > max_pages_listed:
            shown += f", ... (+{len(pages) - max_pages_listed} more)"
        warnings.append(
            f"rotated repeated text (watermark) left in place on {len(pages)} page(s) "
            f"[{shown}] ({reason})"
        )
    return warnings
