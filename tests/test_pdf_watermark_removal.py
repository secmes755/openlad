"""Rotated page-repeated text (watermarks) must be removed from the PDF itself,
before any text is extracted.

Regression coverage for the ingestion defect where a 45-degree watermark on the
text layer was interleaved *inside* body words by line-based extraction
(``S<glyph>upply``). The page stays structurally valid, so nothing downstream
can tell the text is broken -- while exact-match retrieval loses the real tokens
and the vector channel is diluted by the watermark volume. Filtering the
extracted text afterwards cannot recover those tokens.

Fixtures are built in-process with pypdf only, so the open-source suite carries
no vendor document. pypdf/pdfplumber are optional by construction
(requirements-ci.txt keeps them out of the gate), hence the skip guards: these
tests run in the full local environment, not in the minimal CI venv.
"""
import math
from pathlib import Path

import pytest

from core.ingestion.preprocessing.pdf_watermark import (
    _is_rotated,
    _matrix_angle_deg,
    _page_gs_alpha,
    _signature,
    _watermark_score,
    sanitize_pdf_watermark,
    text_integrity_warnings,
)

BODY = "BODYWORD alpha beta gamma"
WATERMARK = "WM-TEXT-WATERMARK"


def _require_pdf_stack():
    """pypdf (writer) + pdfplumber (reader) or skip this module's tests."""
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pdfplumber")
    return pypdf


def _text_block(x: float, y: float, text: str, angle_deg: float = 0.0,
                size: float = 12.0) -> bytes:
    rad = math.radians(angle_deg)
    a, b = math.cos(rad) * size, math.sin(rad) * size
    return (f"BT /F1 {size} Tf {a:.4f} {b:.4f} {-b:.4f} {a:.4f} {x} {y} Tm "
            f"({text}) Tj ET\n").encode("latin-1")


def _watermark_group(angle_deg: float = 45.0, repeats: int = 12,
                     gs_name: str | None = None) -> bytes:
    """One BT..ET group holding a tiled, rotated watermark (as vendors emit it).
    With ``gs_name``, an ExtGState reference sets the group's alpha first."""
    rad = math.radians(angle_deg)
    a, b = math.cos(rad) * 12.0, math.sin(rad) * 12.0
    group = f"/{gs_name} gs\n".encode("latin-1") if gs_name else b""
    group += f"BT /F1 12 Tf {a:.4f} {b:.4f} {-b:.4f} {a:.4f} 40 40 Tm ".encode("latin-1")
    for _ in range(repeats):
        group += f"({WATERMARK}) Tj 200 0 Td ".encode("latin-1")
    return group + b"ET\n"


def _write_pdf(path: Path, pages: int, watermark_pages=(), repeat_horizontal=False,
               body_lines: int = 8, watermark_alpha: float | None = None,
               tagged: bool = False) -> Path:
    """Realistic page shape: a body with real volume plus (optionally) a tiled
    rotated watermark, as vendor documents actually look. A fixture that is
    mostly watermark trips the document-level loss rail -- correctly, since such
    a file is not what the pass is for."""
    pypdf = _require_pdf_stack()
    from pypdf.generic import (
        BooleanObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
    )

    writer = pypdf.PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)  # pypdf exposes no public add_object
    gs_ref = None
    if watermark_alpha is not None:
        gs_ref = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/ExtGState"),
            NameObject("/ca"): FloatObject(watermark_alpha),
            NameObject("/CA"): FloatObject(watermark_alpha),
        }))
    for page_index in range(pages):
        page = writer.add_blank_page(width=595, height=842)
        resources = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        })
        if gs_ref is not None:
            resources[NameObject("/ExtGState")] = DictionaryObject(
                {NameObject("/GS1"): gs_ref})
        page[NameObject("/Resources")] = resources
        content = bytearray()
        for line in range(body_lines):
            text = BODY if line == 0 else f"Body line {line}: register description text."
            content += _text_block(72, 780 - 18 * line, text)
        if repeat_horizontal:
            content += _text_block(72, 810, "RUNNING HEADER")
        if page_index + 1 in watermark_pages:
            content += _watermark_group(gs_name="GS1" if gs_ref is not None else None)
        stream = DecodedStreamObject()
        stream.set_data(bytes(content))
        page.replace_contents(stream)
    if tagged:
        struct_ref = writer._add_object(DictionaryObject(
            {NameObject("/Type"): NameObject("/StructTreeRoot")}))
        writer._root_object[NameObject("/StructTreeRoot")] = struct_ref
        writer._root_object[NameObject("/MarkInfo")] = DictionaryObject(
            {NameObject("/Marked"): BooleanObject(True)})
    with open(path, "wb") as handle:
        writer.write(handle)
    writer.close()
    return path


def _extract(path) -> str:
    import pdfplumber

    with pdfplumber.open(str(path)) as doc:
        return "\n".join(page.extract_text() or "" for page in doc.pages)


def _sha256(path) -> str:
    import hashlib

    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ---------------------------------------------------------------------------
# Detection / removal
# ---------------------------------------------------------------------------
def test_rotated_page_repeated_text_is_detected_and_removed(tmp_path):
    source = _write_pdf(tmp_path / "watermarked.pdf", pages=4,
                        watermark_pages=(1, 2, 3, 4))
    digest_before = _sha256(source)
    cleaned, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")

    assert report["detected"] is True
    assert report["cleaned"] is True
    assert report["pages_modified"] == 4
    assert cleaned != str(source)
    assert report["signatures"][0]["repetition_ratio"] == 1.0

    # The cleaned file reads as clean text. Line-based extraction of the original
    # cannot even spell the watermark (its glyphs are interleaved into every line),
    # which is exactly the defect: the tokens are gone, not merely doubled.
    assert WATERMARK not in _extract(cleaned)
    assert BODY in _extract(cleaned)
    assert _extract(source) != _extract(cleaned)
    assert _sha256(source) == digest_before, "the source file is never rewritten"


def test_body_text_survives_removal(tmp_path):
    source = _write_pdf(tmp_path / "watermarked.pdf", pages=3,
                        watermark_pages=(1, 2, 3))
    cleaned, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")
    text = _extract(cleaned)
    assert report["cleaned"] is True
    assert text.count("Body line 7: register description text.") == 3


def test_rotated_text_on_a_single_page_is_not_a_watermark(tmp_path):
    """min_pages rail: one rotated element in a document is page content."""
    source = _write_pdf(tmp_path / "one_page_rotated.pdf", pages=3,
                        watermark_pages=(1,))
    control = _write_pdf(tmp_path / "control.pdf", pages=3)
    digest_before = _sha256(source)

    cleaned, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")

    assert report["detected"] is False
    assert report["cleaned"] is False
    assert cleaned == str(source)
    # Byte-identical: the pass did not rewrite the file at all, and the rotated
    # text really is in there (it changes the extraction versus the control).
    assert _sha256(source) == digest_before
    assert _extract(source) != _extract(control)


def test_horizontal_repeated_text_is_left_to_the_line_sanitizer(tmp_path):
    """Rotation is required: repeated horizontal headers do not shred word tokens."""
    source = _write_pdf(tmp_path / "headers.pdf", pages=3, repeat_horizontal=True)
    cleaned, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")
    assert report["detected"] is False
    assert cleaned == str(source)
    assert "RUNNING HEADER" in _extract(source)


def test_disabled_pass_touches_nothing(tmp_path):
    source = _write_pdf(tmp_path / "watermarked.pdf", pages=3,
                        watermark_pages=(1, 2, 3))
    cleaned, report = sanitize_pdf_watermark(
        str(source), out_dir=tmp_path / "sanitized", config={"enabled": False})
    assert report["enabled"] is False
    assert report["skipped_reason"] == "disabled"
    assert cleaned == str(source)


def test_parser_returns_clean_text_for_a_watermarked_pdf(tmp_path):
    """End-to-end: the text ingestion stores must not contain shredded body words."""
    _require_pdf_stack()
    from core.ingestion.parser import DocumentParser

    source = _write_pdf(tmp_path / "watermarked.pdf", pages=3,
                        watermark_pages=(1, 2, 3))
    doc = DocumentParser().parse(str(source))

    assert doc.original_path == str(source.absolute()), "the original path is preserved"
    assert doc.metadata["watermark_removal"]["cleaned"] is True
    for page in doc.pages:
        assert BODY in page.raw_text
        assert WATERMARK not in page.raw_text


def test_low_alpha_watermark_scores_higher_and_is_still_removed(tmp_path):
    """Alpha is a soft signal: it raises the score, and removal still works."""
    faint = _write_pdf(tmp_path / "faint.pdf", pages=3, watermark_pages=(1, 2, 3),
                       watermark_alpha=0.1)
    opaque = _write_pdf(tmp_path / "opaque.pdf", pages=3, watermark_pages=(1, 2, 3))

    faint_cleaned, faint_report = sanitize_pdf_watermark(str(faint), out_dir=tmp_path / "s1")
    opaque_cleaned, opaque_report = sanitize_pdf_watermark(str(opaque), out_dir=tmp_path / "s2")

    faint_score = faint_report["signatures"][0]["score"]
    opaque_score = opaque_report["signatures"][0]["score"]
    assert faint_score > opaque_score
    assert faint_report["cleaned"] is True and opaque_report["cleaned"] is True
    assert WATERMARK not in _extract(faint_cleaned)
    assert WATERMARK not in _extract(opaque_cleaned)


def test_cleaned_copy_passes_post_write_verification(tmp_path):
    source = _write_pdf(tmp_path / "watermarked.pdf", pages=3,
                        watermark_pages=(1, 2, 3))
    _, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")
    assert report["verification"] == {"checked": True, "ok": True,
                                      "residual_groups": 0, "error": None}


def test_tagged_pdf_drops_dangling_structure_tree_on_the_cleaned_copy(tmp_path):
    """Removed groups leave dead MCIDs; the cached copy drops the structure."""
    pypdf = _require_pdf_stack()
    source = _write_pdf(tmp_path / "tagged.pdf", pages=3,
                        watermark_pages=(1, 2, 3), tagged=True)
    assert "/StructTreeRoot" in pypdf.PdfReader(str(source)).trailer["/Root"]

    cleaned, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")

    assert report["cleaned"] is True
    assert report["struct_tree_dropped"] is True
    root = pypdf.PdfReader(cleaned).trailer["/Root"]
    assert "/StructTreeRoot" not in root
    assert "/MarkInfo" not in root
    assert BODY in _extract(cleaned)


def test_untagged_pdf_reports_no_structure_change(tmp_path):
    source = _write_pdf(tmp_path / "plain.pdf", pages=3, watermark_pages=(1, 2, 3))
    _, report = sanitize_pdf_watermark(str(source), out_dir=tmp_path / "sanitized")
    assert report["struct_tree_dropped"] is False


# ---------------------------------------------------------------------------
# Pure helpers and reporting
# ---------------------------------------------------------------------------
def test_rotation_predicate_excludes_horizontal_and_vertical():
    assert _is_rotated(45.0, 15.0) is True
    assert _is_rotated(-45.0, 15.0) is True
    assert _is_rotated(0.0, 15.0) is False
    assert _is_rotated(90.0, 15.0) is False
    assert _is_rotated(5.0, 15.0) is False


def test_matrix_angle_is_normalised():
    assert round(_matrix_angle_deg((1.0, 0.0, 0.0, 1.0, 0.0, 0.0))) == 0
    assert round(_matrix_angle_deg((0.7071, 0.7071, -0.7071, 0.7071, 0.0, 0.0))) == 45
    assert round(_matrix_angle_deg((0.0, 1.0, -1.0, 0.0, 0.0, 0.0))) == 90


def test_signature_ignores_font_and_colour_but_tracks_geometry():
    """Resource names are page-local and inherited colour is unstable, so the
    cross-page signature must only use geometry (see the module docstring)."""
    base = {"angle": 45.0, "x": 30.0, "y": 30.0, "text_bytes": 672,
            "show_ops": 12, "font": "/C0_0", "color": "0"}
    other = dict(base, font="/TT0", color="unknown")
    assert _signature(base) == _signature(other)
    assert _signature(base) != _signature(dict(base, x=400.0))
    assert _signature(base) != _signature(dict(base, angle=-45.0))


def test_integrity_warnings_summarise_pages_by_reason():
    report = {
        "detected": True, "cleaned": False, "affected_pages": [1, 2, 3],
        "skipped_reason": "no_page_modified",
        "pages_skipped": [{"page": n, "reason": "would_empty_page"} for n in range(1, 30)],
    }
    warnings = text_integrity_warnings(report)
    assert len(warnings) == 2
    assert "29 page(s)" in warnings[1]
    assert "+21 more" in warnings[1]


def test_no_warnings_when_nothing_was_detected():
    assert text_integrity_warnings({"detected": False, "pages_skipped": []}) == []


def test_extgstate_alpha_is_read_from_page_resources(tmp_path):
    pypdf = _require_pdf_stack()
    source = _write_pdf(tmp_path / "faint.pdf", pages=2, watermark_pages=(1, 2),
                        watermark_alpha=0.1)
    reader = pypdf.PdfReader(str(source))
    assert _page_gs_alpha(reader.pages[0]) == {"/GS1": 0.1}
    plain = _write_pdf(tmp_path / "plain.pdf", pages=1)
    assert _page_gs_alpha(pypdf.PdfReader(str(plain)).pages[0]) == {}


def test_alpha_is_a_soft_score_signal():
    config = {"alpha_signal_threshold": 0.8, "alpha_score_boost": 0.2}
    group = {"angle": 45.0, "color": "0", "show_ops": 1}
    opaque = _watermark_score(dict(group, alpha=1.0), config)
    faint = _watermark_score(dict(group, alpha=0.1), config)
    assert faint == opaque + 0.2
    assert _watermark_score(dict(group, alpha=0.95), config) == opaque


def test_verification_failure_surfaces_as_integrity_warning():
    report = {
        "detected": True, "cleaned": True, "affected_pages": [1, 2],
        "pages_skipped": [],
        "verification": {"checked": True, "ok": False, "residual_groups": 2,
                         "error": None},
    }
    warnings = text_integrity_warnings(report)
    assert any("2 watermark text group(s)" in warning for warning in warnings)
