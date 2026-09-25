"""BUG-9: candidate-page rendering must not materialize the whole PDF.

_render_pdf_pages used to call convert_from_path without first_page /
last_page, rendering every page of the document into memory — the caller
then discarded all non-candidate pages. On large datasheets (hundreds of
pages) this OOMs the ingestion worker for a handful of visual pages.
"""
import sys
import types

from core.ingestion import parser as parser_mod


def _install_fake_pdf2image(monkeypatch, convert):
    """Route the function-local `from pdf2image import convert_from_path`
    through a fake module (same pattern as the fitz stub in
    test_ingestion_logic), so tests run with or without pdf2image."""
    fake = types.ModuleType("pdf2image")
    fake.convert_from_path = convert
    monkeypatch.setitem(sys.modules, "pdf2image", fake)
    monkeypatch.setattr(parser_mod, "HAS_PDF2IMAGE", True)


def _patch_convert(monkeypatch, calls, total_pages=3):
    """Replace pdf2image.convert_from_path with a recording fake that mimics
    the real API: one image per page in [first_page, last_page]."""
    def fake_convert(pdf_path, dpi=100, first_page=None, last_page=None):
        calls.append((first_page, last_page))
        n = (last_page - first_page + 1) if first_page is not None else total_pages
        return [object()] * n

    _install_fake_pdf2image(monkeypatch, fake_convert)


def test_render_scopes_to_candidate_pages(monkeypatch):
    calls = []
    _patch_convert(monkeypatch, calls)
    p = parser_mod.DocumentParser()
    images = p._render_pdf_pages("big.pdf", dpi=72, pages=[3, 4, 5, 9, 20])
    assert sorted(images) == [3, 4, 5, 9, 20]
    # contiguous runs batched into one pdftoppm call each; the whole
    # document is never rendered
    assert calls == [(3, 5), (9, 9), (20, 20)]


def test_render_empty_candidates_renders_nothing(monkeypatch):
    calls = []
    _patch_convert(monkeypatch, calls)
    p = parser_mod.DocumentParser()
    assert p._render_pdf_pages("big.pdf", dpi=72, pages=[]) == {}
    assert calls == []


def test_render_default_still_renders_all(monkeypatch):
    """pages=None keeps the whole-document behavior for other callers."""
    calls = []
    _patch_convert(monkeypatch, calls, total_pages=3)
    p = parser_mod.DocumentParser()
    images = p._render_pdf_pages("doc.pdf", dpi=72)
    assert sorted(images) == [1, 2, 3]
    assert calls == [(None, None)]


def test_render_run_failure_keeps_other_runs(monkeypatch):
    """A failing run must not lose the other candidate pages."""
    def flaky_convert(pdf_path, dpi=100, first_page=None, last_page=None):
        if first_page == 9:
            raise RuntimeError("pdftoppm choked on page 9")
        return [object()] * (last_page - first_page + 1)

    _install_fake_pdf2image(monkeypatch, flaky_convert)
    p = parser_mod.DocumentParser()
    images = p._render_pdf_pages("big.pdf", dpi=72, pages=[3, 9, 10])
    assert sorted(images) == [3, 10]
