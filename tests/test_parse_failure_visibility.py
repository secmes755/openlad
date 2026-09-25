"""Parse-failure visibility (BUG-7.2/7.3): a parser that fails partway or
wholesale must mark the document degraded via `parse_warnings` metadata —
never silently return a partial/empty document that ships as 'verified'.

The PDF main loop guards text/table extraction per page, but outline
building, section-title extraction and other unguarded calls can still
abort the loop mid-document; previously the outer except returned the
partial document with no signal at all.
"""
import sys
import types

# builder.py top-level imports PIL/numpy (via layout), which the CI venv
# intentionally lacks — same stub pattern as test_embedding_failure_visibility.
try:
    import PIL  # noqa: F401
    import PIL.Image  # noqa: F401
except ImportError:
    _pil_image = types.ModuleType("PIL.Image")
    _pil_image.Image = type("Image", (), {})
    _pil = types.ModuleType("PIL")
    _pil.Image = _pil_image
    for _name, _mod in (("PIL", _pil), ("PIL.Image", _pil_image)):
        sys.modules.setdefault(_name, _mod)
try:
    import numpy  # noqa: F401
except ImportError:
    _np = types.ModuleType("numpy")
    _np.ndarray = type("ndarray", (), {})
    sys.modules.setdefault("numpy", _np)

from core.ingestion import parser as parser_mod  # noqa: E402
from core.ingestion.builder import DocumentIndexBuilder  # noqa: E402


class _FakePlumberPage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text

    def extract_tables(self):
        return []


class _FakePlumberDoc:
    def __init__(self, texts):
        self.pages = [_FakePlumberPage(t) for t in texts]

    def close(self):
        pass


class _FakePypdfPage:
    def get(self, key):
        return None  # no /Resources -> no images -> TEXT page


class _FakePypdfReader:
    def __init__(self, n):
        self.pages = [_FakePypdfPage() for _ in range(n)]
        self.metadata = None


def _make_parser(monkeypatch):
    """Real DocumentParser with outline/TOC/render seams isolated from fakes."""
    p = parser_mod.DocumentParser()
    monkeypatch.setattr(p, "_build_outline_map", lambda reader: {})
    monkeypatch.setattr(p, "_build_full_toc", lambda reader: [])
    monkeypatch.setattr(p, "_render_pdf_pages", lambda *a, **k: {})
    return p


def _patch_pdf_machinery(monkeypatch, n_pages=3, reader_error=None):
    """Replace the pdfplumber/pypdf/watermark/model seams with deterministic
    fakes so _parse_pdf runs fully offline, in any dependency environment."""
    monkeypatch.setattr(parser_mod, "HAS_PYPDF", True)
    monkeypatch.setattr(parser_mod, "HAS_PDFPLUMBER", True)
    fake_pypdf = types.ModuleType("pypdf")
    if reader_error is not None:
        def _raise(path):
            raise reader_error
        fake_pypdf.PdfReader = _raise
    else:
        fake_pypdf.PdfReader = lambda path: _FakePypdfReader(n_pages)
    monkeypatch.setattr(parser_mod, "pypdf", fake_pypdf, raising=False)
    fake_plumber = types.ModuleType("pdfplumber")
    plumber_doc = _FakePlumberDoc(
        [f"page {i} body text, enough characters." for i in range(1, n_pages + 1)]
    )
    fake_plumber.open = lambda path: plumber_doc
    monkeypatch.setattr(parser_mod, "pdfplumber", fake_plumber, raising=False)
    monkeypatch.setattr(parser_mod, "sanitize_pdf_watermark", lambda p: (p, {}))
    monkeypatch.setattr(parser_mod, "text_integrity_warnings", lambda report: [])
    monkeypatch.setattr(
        parser_mod, "get_model_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no model in test")),
    )


# --------------------------------------------------------------------------
# BUG-7.3: PDF mid-document crash returns a partial document silently
# --------------------------------------------------------------------------

def test_pdf_partial_crash_carries_parse_warning(monkeypatch, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-fake")
    _patch_pdf_machinery(monkeypatch, n_pages=3)
    p = _make_parser(monkeypatch)
    orig = p._extract_section_title

    def boom(text, page_num, outline_map):
        if page_num == 2:
            raise RuntimeError("simulated mid-document crash")
        return orig(text, page_num, outline_map)

    monkeypatch.setattr(p, "_extract_section_title", boom)
    doc = p._parse_pdf(f)
    assert len(doc.pages) == 1, "pages completed before the crash must be kept"
    warnings = doc.metadata.get("parse_warnings")
    assert warnings, "a partial document must carry a parse warning"
    assert any("1" in w and "3" in w for w in warnings), \
        "the warning should state how many pages of the total survived"


def test_pdf_total_crash_with_fallback_carries_parse_warning(monkeypatch, tmp_path):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-fake")
    _patch_pdf_machinery(monkeypatch, reader_error=RuntimeError("totally corrupted"))
    p = _make_parser(monkeypatch)
    monkeypatch.setattr(
        parser_mod.DocumentParser, "_extract_pages_with_pymupdf",
        staticmethod(lambda path: ["recovered page one", "recovered page two"]),
    )
    doc = p._parse_pdf(f)
    assert len(doc.pages) == 2
    assert doc.pages[0].content_dict.get("fallback") == "pymupdf"
    assert doc.metadata.get("parse_warnings"), \
        "fallback recovery is still degraded content and must be flagged"


def test_pdf_total_crash_without_fallback_carries_parse_warning(monkeypatch, tmp_path):
    f = tmp_path / "dead.pdf"
    f.write_bytes(b"%PDF-fake")
    _patch_pdf_machinery(monkeypatch, reader_error=RuntimeError("totally corrupted"))
    p = _make_parser(monkeypatch)
    monkeypatch.setattr(
        parser_mod.DocumentParser, "_extract_pages_with_pymupdf",
        staticmethod(lambda path: []),
    )
    doc = p._parse_pdf(f)
    assert len(doc.pages) == 1  # placeholder page
    assert "PDF parsing failed" in doc.pages[0].raw_text
    assert doc.metadata.get("parse_warnings"), \
        "a placeholder-only document must be flagged, not shipped verified"


# --------------------------------------------------------------------------
# BUG-7.2: Excel/PPT parse failure returns an empty document silently
# --------------------------------------------------------------------------

def test_excel_failure_carries_parse_warning(monkeypatch, tmp_path):
    f = tmp_path / "book.xlsx"
    f.write_bytes(b"fake workbook bytes")
    monkeypatch.setattr(parser_mod, "HAS_EXCEL", True)

    def _raise_excel_file(path):
        raise RuntimeError("corrupt workbook")

    monkeypatch.setattr(
        parser_mod, "pd", types.SimpleNamespace(ExcelFile=_raise_excel_file),
        raising=False,
    )
    doc = parser_mod.DocumentParser()._parse_excel(f)
    assert doc.pages == []
    assert doc.metadata.get("parse_warnings"), \
        "a 0-page Excel document must not ship verified"


def test_ppt_failure_carries_parse_warning(monkeypatch, tmp_path):
    f = tmp_path / "slides.pptx"
    f.write_bytes(b"fake deck bytes")
    monkeypatch.setattr(parser_mod, "HAS_PPT", True)

    def _raise_presentation(path):
        raise RuntimeError("corrupt deck")

    monkeypatch.setattr(parser_mod, "Presentation", _raise_presentation,
                        raising=False)
    doc = parser_mod.DocumentParser()._parse_ppt(f)
    assert doc.pages == []
    assert doc.metadata.get("parse_warnings"), \
        "a 0-slide deck must not ship verified"


# --------------------------------------------------------------------------
# Builder wiring: parse_warnings must drive the degraded status
# --------------------------------------------------------------------------

def test_builder_collects_parse_warnings():
    warnings = DocumentIndexBuilder._collect_ingest_warnings(
        embed_warnings=None,
        parsed_metadata={"parse_warnings": ["PDF parsing aborted: 1 of 3 pages"]},
    )
    assert warnings == ["PDF parsing aborted: 1 of 3 pages"]
