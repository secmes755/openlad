"""BUG-6: OCR temp files must not collide across concurrent ingests.

_ocr_pipeline writes temp_p{page}.png / ocr_p{page}.png — names derived
only from the tenant images dir and page number. Two documents ingested
concurrently for the SAME tenant hit the same path at the same page:
one pipeline overwrites the other's OCR input mid-read, and its cleanup
unlinks the file the other is about to read.

Fix: give each pipeline instance's temp files a unique suffix.
"""
import threading

import numpy as np
import pytest
from PIL import Image

from core.ingestion.preprocessing import (
    DocumentPreprocessor, PagePreprocessResult,
)


class _RecordingCorrector:
    def __init__(self, barrier, recorded):
        self._barrier = barrier
        self._recorded = recorded
    def correct(self, path):
        self._recorded.append(("temp", str(path)))
        self._barrier.wait(timeout=10)  # force both pipelines to overlap here
        return np.zeros((4, 4, 3), dtype=np.uint8), {}


class _RecordingOCR:
    def __init__(self, recorded):
        self._recorded = recorded
    def recognize(self, path, page_num=None):
        self._recorded.append(("ocr", str(path)))
        return "text", [], {}
    def release(self):
        pass


def _make_preprocessor(images_dir, corrector, ocr):
    pp = DocumentPreprocessor.__new__(DocumentPreprocessor)
    pp.images_dir = images_dir
    pp.corrector = corrector
    pp.ocr_engine = ocr
    return pp


def test_concurrent_same_page_temp_paths_do_not_collide(tmp_path):
    barrier = threading.Barrier(2)
    recorded = []
    lock = threading.Lock()

    class SyncCorrector(_RecordingCorrector):
        def correct(self, path):
            with lock:
                self._recorded.append(("temp", str(path)))
            self._barrier.wait(timeout=10)
            return np.zeros((4, 4, 3), dtype=np.uint8), {}

    corr1, corr2 = SyncCorrector(barrier, recorded), SyncCorrector(barrier, recorded)
    ocr = _RecordingOCR(recorded)
    pp1 = _make_preprocessor(tmp_path, corr1, ocr)
    pp2 = _make_preprocessor(tmp_path, corr2, ocr)

    img = Image.new("RGB", (8, 8), "white")
    errors = []

    def run(pp):
        try:
            pp._ocr_pipeline(img, 3, PagePreprocessResult())
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=run, args=(pp1,))
    t2 = threading.Thread(target=run, args=(pp2,))
    t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)

    assert not errors
    temp_paths = [p for kind, p in recorded if kind == "temp"]
    assert len(temp_paths) == 2
    assert temp_paths[0] != temp_paths[1], (
        f"concurrent pipelines share a temp path: {temp_paths}")


def test_ocr_temp_files_cleaned_up(tmp_path):
    recorded = []
    class C:
        def correct(self, path):
            recorded.append(str(path))
            return np.zeros((4, 4, 3), dtype=np.uint8), {}
    class O:
        def recognize(self, path, page_num=None):
            return "text", [], {}
    pp = _make_preprocessor(tmp_path, C(), O())
    result = pp._ocr_pipeline(Image.new("RGB", (8, 8)), 5, PagePreprocessResult())
    assert result.raw_text == "text"
    leftovers = [p for p in tmp_path.iterdir()
                 if p.name.startswith(("temp_p", "ocr_p"))]
    assert leftovers == [], f"temp files not cleaned: {leftovers}"
