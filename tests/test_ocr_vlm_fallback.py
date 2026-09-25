"""VLM OCR fallback engine must actually load settings and call the endpoint.

Contract under test: OCREngine._recognize_vlm() reads core.config.settings
(inside the function, deliberately lazy). The import used to climb one
package level too far (....config), raising ImportError beyond the
top-level package; the blanket except swallowed it, so the VLM fallback
permanently returned ("", [], {"error": ...}) — a dead engine that only
ever produced hollow pages. Pinned behavior: with a mocked endpoint the
fallback returns the extracted text with engine="vlm" metadata.
"""
from unittest.mock import Mock, patch

import pytest


def _real_pil_image():
    """Return real PIL.Image or skip (CI stub-injection guard, same as
    tests/test_ocr_endpoint.py)."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")
    if not hasattr(Image, "new"):
        pytest.skip("PIL.Image stubbed (Pillow absent in this env)")
    return Image


def test_vlm_fallback_returns_extracted_text(tmp_path):
    Image = _real_pil_image()
    from core.ingestion.preprocessing.ocr_engine import OCREngine

    img_file = tmp_path / "page1.png"
    Image.new("RGB", (16, 16), "white").save(img_file, "PNG")

    fake_resp = Mock(status_code=200)
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "extracted page text"}}]
    }

    engine = OCREngine()
    with patch(
        "core.ingestion.preprocessing.ocr_engine.requests.post",
        return_value=fake_resp,
    ) as post:
        text, results, meta = engine._recognize_vlm(str(img_file), 1)

    assert post.called, "VLM endpoint was never called"
    assert text == "extracted page text"
    assert meta.get("engine") == "vlm"
    assert "error" not in meta
    assert len(results) == 1 and results[0].text == "extracted page text"


def test_vlm_fallback_surfaces_endpoint_errors(tmp_path):
    """A failing endpoint must surface an error, not silently pass."""
    Image = _real_pil_image()
    from core.ingestion.preprocessing.ocr_engine import OCREngine

    img_file = tmp_path / "page1.png"
    Image.new("RGB", (16, 16), "white").save(img_file, "PNG")

    fake_resp = Mock(status_code=500)
    engine = OCREngine()
    with patch(
        "core.ingestion.preprocessing.ocr_engine.requests.post",
        return_value=fake_resp,
    ):
        text, results, meta = engine._recognize_vlm(str(img_file), 1)

    assert text == "" and results == []
    assert "error" in meta
