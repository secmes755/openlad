"""Image preprocessing helpers — quality metrics and skew detection.

``image_corrector`` (296 lines) had no tests. It runs before OCR on every
scanned page (``preprocessing/__init__.py`` calls ``correct()`` unconditionally),
so a silent misdetection here changes what OCR is given and therefore what ends
up in the index.

These tests use synthetic images with known properties, which makes the
detectors' behaviour checkable without shipping binary fixtures:

  * a blank image must not be reported as skewed;
  * a page rotated by a known angle must be measured back within tolerance;
  * low-DPI detection must fire on small images and stay quiet on large ones;
  * the quality report must always carry the keys downstream code reads.
"""

import cv2
import numpy as np
import pytest

from core.ingestion.preprocessing.image_corrector import ImageCorrector


@pytest.fixture
def corrector():
    return ImageCorrector()


def blank(h=800, w=600, value=255):
    return np.full((h, w, 3), value, dtype=np.uint8)


def text_like(angle=0.0, h=900, w=700):
    """Horizontal dark bars on white — stands in for text lines."""
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    for y in range(60, h - 60, 40):
        cv2.line(img, (60, y), (w - 60, y), (0, 0, 0), 3)
    if angle:
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        img = cv2.warpAffine(img, m, (w, h), borderValue=(255, 255, 255))
    return img


class TestQualityMetricsKeys:
    def test_report_carries_every_key_downstream_reads(self, corrector):
        m = corrector._analyze_quality(text_like())
        for key in ("estimated_dpi", "low_dpi", "skew_angle",
                    "needs_deskew", "perspective_distortion",
                    "brightness_std", "uneven_lighting"):
            assert key in m, f"missing quality key {key!r}"

    def test_dpi_estimate_scales_with_image_size(self, corrector):
        small = corrector._analyze_quality(blank(300, 300))
        large = corrector._analyze_quality(blank(2400, 1700))
        assert large["estimated_dpi"] > small["estimated_dpi"]

    def test_small_image_is_flagged_low_dpi(self, corrector):
        assert bool(corrector._analyze_quality(blank(300, 200))["low_dpi"]) is True

    def test_large_scan_is_not_flagged_low_dpi(self, corrector):
        assert bool(corrector._analyze_quality(blank(2400, 1700))["low_dpi"]) is False

    def test_uniform_image_has_low_brightness_variance(self, corrector):
        m = corrector._analyze_quality(blank())
        assert m["brightness_std"] < 1.0
        assert bool(m["uneven_lighting"]) is False

    def test_half_dark_image_is_flagged_uneven(self, corrector):
        img = blank()
        img[:, : img.shape[1] // 2] = 20
        m = corrector._analyze_quality(img)
        assert bool(m["uneven_lighting"]) is True


class TestSkewDetection:
    def test_blank_page_reports_no_skew(self, corrector):
        gray = cv2.cvtColor(blank(), cv2.COLOR_BGR2GRAY)
        assert corrector._detect_skew_angle(gray) == 0.0

    def test_straight_text_reports_near_zero(self, corrector):
        gray = cv2.cvtColor(text_like(0.0), cv2.COLOR_BGR2GRAY)
        assert abs(corrector._detect_skew_angle(gray)) < 2.0

    @pytest.mark.parametrize("angle", [5.0, -5.0, 10.0])
    def test_known_rotation_is_measured_back(self, corrector, angle):
        gray = cv2.cvtColor(text_like(angle), cv2.COLOR_BGR2GRAY)
        measured = corrector._detect_skew_angle(gray)
        # Hough on synthetic bars is accurate but not exact; 2 degrees is enough
        # to prove the direction and rough magnitude are right.
        assert abs(abs(measured) - abs(angle)) < 2.0, (
            f"rotation {angle}° measured as {measured}°"
        )

    def test_skew_sign_follows_rotation_direction(self, corrector):
        pos = corrector._detect_skew_angle(cv2.cvtColor(text_like(8.0), cv2.COLOR_BGR2GRAY))
        neg = corrector._detect_skew_angle(cv2.cvtColor(text_like(-8.0), cv2.COLOR_BGR2GRAY))
        assert pos * neg <= 0, (
            f"opposite rotations produced same-signed angles ({pos}, {neg})"
        )

    def test_needs_deskew_threshold(self, corrector):
        straight = corrector._analyze_quality(text_like(0.0))
        tilted = corrector._analyze_quality(text_like(10.0))
        assert bool(straight["needs_deskew"]) is False
        assert bool(tilted["needs_deskew"]) is True


class TestPerspectiveDetection:
    def test_blank_page_reports_no_distortion(self, corrector):
        gray = cv2.cvtColor(blank(), cv2.COLOR_BGR2GRAY)
        assert corrector._detect_perspective(gray) is False

    def test_returns_bool(self, corrector):
        gray = cv2.cvtColor(text_like(), cv2.COLOR_BGR2GRAY)
        assert isinstance(corrector._detect_perspective(gray), bool)


class TestCorrectShortCircuits:
    def test_missing_file_degrades_instead_of_raising(self, corrector, tmp_path):
        """One unreadable page image must not abort the whole document.

        ``correct()`` wraps its work in try/except and falls back to
        ``Image.open``; when the file cannot be read at all that fallback used
        to raise out of the handler, so a single bad page took down ingestion
        for every other page. It now returns an empty array plus an ``unreadable``
        marker the caller can skip on.
        """
        img, report = corrector.correct(str(tmp_path / "does_not_exist.png"))
        assert isinstance(report, dict)
        assert report.get("unreadable") is True
        assert img is not None

    def test_unreadable_report_names_the_cause(self, corrector, tmp_path):
        _, report = corrector.correct(str(tmp_path / "nope.png"))
        assert report.get("error"), "the original failure must be reported"
        assert report.get("unreadable_reason"), "the fallback failure must be reported"

    def test_clean_image_is_returned_unchanged(self, corrector, tmp_path):
        """A straight, well-lit page must not be reskewed — resampling costs
        OCR accuracy on already-good input."""
        p = tmp_path / "clean.png"
        cv2.imwrite(str(p), text_like(0.0))
        out, report = corrector.correct(str(p))
        assert out is not None
        assert out.shape == text_like(0.0).shape
        assert bool(report.get("needs_deskew")) is False
