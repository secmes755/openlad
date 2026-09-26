"""OCR capability gates: a missing tesseract binary must be detected up front."""
from PIL import Image

from core.config import settings
from core.ingestion.preprocessing import DocumentPreprocessor
from core.ingestion.preprocessing import ocr_engine as oe


def test_auto_engine_falls_back_when_tesseract_binary_missing(monkeypatch):
    monkeypatch.setattr(oe, "HAS_TESSERACT", True)
    monkeypatch.setattr(oe, "TESSERACT_BINARY_AVAILABLE", False)

    engine = oe.OCREngine({"engine": "auto", "language": "zh_en"})

    assert engine._select_engine() == "vlm"


def test_tesseract_engine_is_not_selected_without_binary(monkeypatch):
    monkeypatch.setattr(oe, "TESSERACT_BINARY_AVAILABLE", False)

    engine = oe.OCREngine({"engine": "tesseract", "language": "en"})

    assert engine._select_engine() == "vlm"


def test_preprocess_fails_fast_when_no_ocr_engine_exists(monkeypatch, tmp_path):
    monkeypatch.setitem(settings.CHART_CONFIG, "enabled", False)
    monkeypatch.setitem(settings.OCR_CONFIG, "engine", "auto")
    monkeypatch.setattr(oe, "TESSERACT_BINARY_AVAILABLE", False)

    preprocessor = DocumentPreprocessor(images_dir=str(tmp_path))
    page = Image.new("RGB", (40, 40), "white")

    result = preprocessor.preprocess_pdf_page(page, page_num=1, direct_text="", doc_id="d")

    assert result.raw_text == ""
    assert result.text_source == "direct_extract"
    assert result.quality_metrics["ocr_unavailable"] is True
