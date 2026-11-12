"""Lazy page-image cache, source capability flags, and atomic repair."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from core.ingestion.page_images import (
    document_supports_page_images,
    ensure_page_image,
    is_placeholder_image,
)
from core.retrieval.retriever import SearchResult, SegmentMerger


def _textured_image():
    image = Image.new('RGB', (800, 1000), 'white')
    image.putpixel((0, 0), (0, 0, 0))
    return image


def test_page_image_capability_requires_a_pdf_source():
    assert document_supports_page_images({'original_path': '/x/manual.PDF'})
    assert not document_supports_page_images({'original_path': '/x/book.xlsx'})
    assert not document_supports_page_images({})
    assert not document_supports_page_images(None)


def test_placeholder_detection_does_not_reject_a_real_nonuniform_page(tmp_path):
    placeholder = tmp_path / 'placeholder.png'
    Image.new('RGB', (800, 1000), 'white').save(placeholder)
    real = tmp_path / 'real.png'
    _textured_image().save(real)

    assert is_placeholder_image(placeholder)
    assert not is_placeholder_image(real)
    assert is_placeholder_image(tmp_path / 'missing.png')


def test_repair_is_atomic_and_concurrent_callers_leave_a_valid_image(monkeypatch, tmp_path):
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'synthetic')
    target = tmp_path / 'images' / 'doc_p1.png'
    monkeypatch.setattr(
        'core.ingestion.parser.DocumentParser._render_with_pdfium',
        lambda self, path, pages, dpi: {1: _textured_image()},
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(
            lambda _: ensure_page_image(str(source), 1, target), range(4),
        ))

    assert any(outcomes)
    assert not is_placeholder_image(target)
    assert list(target.parent.glob('*.tmp')) == []


def test_missing_source_cannot_be_repaired(tmp_path):
    assert not ensure_page_image(
        str(tmp_path / 'missing.pdf'), 1, tmp_path / 'target.png',
    )


def test_merger_marks_non_pdf_sources_as_text_only(monkeypatch):
    merger = SegmentMerger()
    monkeypatch.setattr(merger, 'metadata_db', SimpleNamespace(
        get_document=lambda doc_id: {
            'title': 'Workbook', 'filename': 'book.xlsx',
            'original_path': '/x/book.xlsx',
        },
        execute=lambda *args: SimpleNamespace(fetchall=lambda: []),
    ))
    result = SearchResult(
        'doc', page_num=1, score=10, content='cell text', title='Workbook',
    )

    _, sources = merger.merge([result], max_context_chars=1000, query='cell', industry_hint='auto')

    assert sources[0]['has_page_image'] is False


def test_frontend_hides_only_explicitly_unsupported_page_images():
    source = Path('frontend/web/dist/js/app.js').read_text()
    assert 's.doc_id && pages.length && s.has_page_image !== false' in source
    assert '<span class="source-link"' in source
