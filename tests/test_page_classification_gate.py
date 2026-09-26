"""VLM page classification must be gated by the main-LLM vision switch.

When semantic vision is disabled and OCR mode is off, candidate pages must stay
TEXT without rendering page bitmaps or calling the main LLM.
"""
from core.config import settings
from core.ingestion import parser as parser_mod
from tests.test_parse_failure_visibility import _make_parser, _patch_pdf_machinery


class _FakeImageObject:
    def get_object(self):
        return self

    def get(self, key):
        return "/Image" if key == "/Subtype" else None


class _FakeImagePage:
    def get(self, key):
        if key != "/Resources":
            return None
        return {"/XObject": {"/Im1": _FakeImageObject()}}


class _FakeImageReader:
    pages = [_FakeImagePage()]
    metadata = None


def test_candidate_pages_skip_render_and_classification_when_vision_disabled(monkeypatch, tmp_path):
    f = tmp_path / "imagey.pdf"
    f.write_bytes(b"%PDF-fake")
    _patch_pdf_machinery(monkeypatch, n_pages=1)
    parser = _make_parser(monkeypatch)

    monkeypatch.setattr(parser_mod.pypdf, "PdfReader", lambda path: _FakeImageReader())
    monkeypatch.setitem(settings.CHART_CONFIG, "enabled", False)
    monkeypatch.setitem(settings.OCR_CONFIG, "enabled", False)

    render_calls = []
    monkeypatch.setattr(parser, "_render_pdf_pages", lambda *a, **k: render_calls.append((a, k)) or {})
    monkeypatch.setattr(
        parser, "_classify_one_page",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("classifier must not run")),
    )

    doc = parser._parse_pdf(f)

    assert render_calls == [], "no page bitmaps should be rendered for a disabled classifier"
    assert doc.pages[0].content_dict["page_class"] == "TEXT"
    assert doc.pages[0].page_image is None
