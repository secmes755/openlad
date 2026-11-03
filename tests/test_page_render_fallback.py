"""Page rendering must not depend on the poppler binaries.

pdf2image shells out to poppler's pdftoppm. On a machine without poppler every
render fails, every source page keeps no image, and the browser's Source view
shows nothing at all. pypdfium2 ships its own renderer and now backs pdf2image
up, so a machine with no poppler still produces page images.

Regression guard: pypdfium2 must be driven through get_page(i). Indexing the
document in a loop returned the same page over and over and produced a whole
document of identical images.
"""
import hashlib

import pytest

from core.ingestion import parser as parser_mod
from core.ingestion.parser import DocumentParser

fitz = pytest.importorskip("fitz", reason="PyMuPDF builds the fixture PDF")
pytest.importorskip("pypdfium2", reason="the fallback renderer under test")


@pytest.fixture
def three_page_pdf(tmp_path):
    """Three pages with visibly different content."""
    path = tmp_path / "three.pdf"
    doc = fitz.open()
    for text in ("Page one alpha", "Page two bravo", "Page three charlie"):
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=24)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def no_poppler(monkeypatch):
    monkeypatch.setattr(parser_mod, "HAS_PDF2IMAGE", False)


def test_pages_render_without_pdf2image(three_page_pdf, no_poppler):
    images = DocumentParser()._render_pdf_pages(three_page_pdf, dpi=72, pages=[1, 2, 3])
    assert sorted(images) == [1, 2, 3]


def test_each_page_is_a_distinct_image(three_page_pdf, no_poppler):
    images = DocumentParser()._render_pdf_pages(three_page_pdf, dpi=72, pages=[1, 2, 3])
    assert len(images) == 3, "no pages rendered, so uniqueness would pass vacuously"
    digests = {p: hashlib.md5(img.tobytes()).hexdigest() for p, img in images.items()}
    assert len(set(digests.values())) == len(digests), (
        "pages rendered identically: the fallback must use get_page(i), not pdf[i]"
    )


def test_single_page_falls_back(three_page_pdf, no_poppler):
    img = DocumentParser()._render_single_page(three_page_pdf, 2, dpi=72)
    assert img is not None
    assert img.size[0] > 0 and img.size[1] > 0


def test_page_out_of_range_is_skipped(three_page_pdf, no_poppler):
    images = DocumentParser()._render_pdf_pages(three_page_pdf, dpi=72, pages=[99])
    assert images == {}


def test_whole_document_falls_back(three_page_pdf, no_poppler):
    images = DocumentParser()._render_pdf_pages(three_page_pdf, dpi=72, pages=None)
    assert sorted(images) == [1, 2, 3]
