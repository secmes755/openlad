"""
V4 Image Corrector - CPU-based processing using OpenCV
Supports: perspective correction, deskewing, lighting equalization, denoising, binarization
"""
import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False
    logging.warning("OpenCV not installed, image correction disabled")

logger = logging.getLogger(__name__)


class ImageCorrector:
    """Image corrector"""

    def __init__(self):
        self.enabled = HAS_OPENCV
        if not self.enabled:
            logger.warning("ImageCorrector disabled - OpenCV not available")

    def correct(self, image_path: str) -> tuple[np.ndarray, dict[str, Any]]:
        """
        Apply correction processing to an image

        Returns:
            corrected_image: Corrected image (numpy array, RGB)
            metrics: Quality metrics dictionary
        """
        if not self.enabled:
            # Return original image directly
            img = Image.open(image_path).convert('RGB')
            return np.array(img), {"skipped": True, "reason": "opencv_not_available"}

        try:
            img = cv2.imread(str(image_path))
            if img is None:
                raise ValueError(f"Cannot read image: {image_path}")

            metrics = self._analyze_quality(img)
            corrected = img.copy()

            # 1. Perspective correction (if perspective distortion detected)
            if metrics.get("perspective_distortion", False):
                corrected = self._correct_perspective(corrected)
                metrics["perspective_corrected"] = True

            # 2. Deskew (if skew detected)
            if metrics.get("skew_angle", 0) > 2.0:
                corrected = self._deskew(corrected)
                metrics["deskewed"] = True

            # 3. Lighting equalization (if lighting is uneven)
            if metrics.get("uneven_lighting", False):
                corrected = self._equalize_lighting(corrected)
                metrics["lighting_equalized"] = True

            # 4. Denoise (if DPI is low)
            if metrics.get("low_dpi", False):
                corrected = self._denoise(corrected)
                metrics["denoised"] = True

            # 5. Binarization (optimize for OCR)
            # Use adaptive threshold to preserve more detail
            corrected = self._adaptive_threshold(corrected)
            metrics["binarized"] = True

            # Convert to RGB
            corrected_rgb = cv2.cvtColor(corrected, cv2.COLOR_BGR2RGB)

            logger.info(f"Image corrected: {Path(image_path).name}, metrics={metrics}")
            return corrected_rgb, metrics

        except Exception as e:
            logger.error(f"Image correction failed {image_path}: {e}")
            img = Image.open(image_path).convert('RGB')
            return np.array(img), {"error": str(e)}

    def _analyze_quality(self, img: np.ndarray) -> dict[str, Any]:
        """Analyze image quality"""
        metrics = {}
        h, w = img.shape[:2]

        # Detect DPI (estimate based on image dimensions, assuming A4 size)
        estimated_dpi = min(w, h) / 8.27  # A4 width 8.27 inches
        metrics["estimated_dpi"] = estimated_dpi
        metrics["low_dpi"] = estimated_dpi < 200

        # Detect skew angle
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        skew_angle = self._detect_skew_angle(gray)
        metrics["skew_angle"] = abs(skew_angle)
        metrics["needs_deskew"] = abs(skew_angle) > 2.0

        # Detect perspective distortion (simple: check if corners form a rectangle)
        metrics["perspective_distortion"] = self._detect_perspective(gray)

        # Detect lighting uniformity
        std_dev = np.std(gray)
        metrics["brightness_std"] = std_dev
        metrics["uneven_lighting"] = std_dev > 60

        return metrics

    def _detect_skew_angle(self, gray: np.ndarray) -> float:
        """Detect text line skew angle (based on Hough transform)"""
        try:
            # Binarization
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            # Detect text lines
            lines = cv2.HoughLinesP(binary, 1, np.pi/180, 100, minLineLength=100, maxLineGap=10)

            if lines is None or len(lines) == 0:
                return 0.0

            angles = []
            for line in lines:
                x1, y1, x2, y2 = line[0]
                if x2 - x1 != 0:
                    angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                    # Only consider near-horizontal lines
                    if abs(angle) < 45:
                        angles.append(angle)

            if not angles:
                return 0.0

            return np.median(angles)
        except Exception as e:
            logger.warning(f"Skew detection failed: {e}")
            return 0.0

    def _detect_perspective(self, gray: np.ndarray) -> bool:
        """Detect perspective distortion (simple version)"""
        try:
            edges = cv2.Canny(gray, 50, 150)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if not contours:
                return False

            # Find the largest contour
            largest = max(contours, key=cv2.contourArea)
            epsilon = 0.02 * cv2.arcLength(largest, True)
            approx = cv2.approxPolyDP(largest, epsilon, True)

            # If the approximated polygon is not a quadrilateral, there may be perspective distortion
            if len(approx) != 4:
                return False

            # Check if angles are close to 90 degrees
            angles = []
            for i in range(4):
                p1 = approx[i][0]
                p2 = approx[(i+1)%4][0]
                p3 = approx[(i+2)%4][0]
                v1 = p1 - p2
                v2 = p3 - p2
                cos_angle = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
                angle = np.degrees(np.arccos(np.clip(cos_angle, -1, 1)))
                angles.append(angle)

            max_deviation = max(abs(a - 90) for a in angles)
            return max_deviation > 5.0

        except Exception as e:
            logger.warning(f"Perspective detection failed: {e}")
            return False

    def _correct_perspective(self, img: np.ndarray) -> np.ndarray:
        """Perspective correction (based on edge detection + affine transform)"""
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if not contours:
                return img

            largest = max(contours, key=cv2.contourArea)
            epsilon = 0.02 * cv2.arcLength(largest, True)
            approx = cv2.approxPolyDP(largest, epsilon, True)

            if len(approx) != 4:
                return img

            # Sort four corner points: top-left, top-right, bottom-right, bottom-left
            pts = approx.reshape(4, 2).astype(np.float32)
            rect = np.zeros((4, 2), dtype=np.float32)

            s = pts.sum(axis=1)
            rect[0] = pts[np.argmin(s)]  # top-left
            rect[2] = pts[np.argmax(s)]  # bottom-right

            diff = np.diff(pts, axis=1)
            rect[1] = pts[np.argmin(diff)]  # top-right
            rect[3] = pts[np.argmax(diff)]  # bottom-left

            # Calculate output dimensions
            width_a = np.linalg.norm(rect[2] - rect[3])
            width_b = np.linalg.norm(rect[1] - rect[0])
            max_width = max(int(width_a), int(width_b))

            height_a = np.linalg.norm(rect[1] - rect[2])
            height_b = np.linalg.norm(rect[0] - rect[3])
            max_height = max(int(height_a), int(height_b))

            # Affine transform
            dst = np.array([
                [0, 0],
                [max_width - 1, 0],
                [max_width - 1, max_height - 1],
                [0, max_height - 1]
            ], dtype=np.float32)

            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(img, M, (max_width, max_height))

            return warped
        except Exception as e:
            logger.warning(f"Perspective correction failed: {e}")
            return img

    def _deskew(self, img: np.ndarray) -> np.ndarray:
        """Deskew"""
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            angle = self._detect_skew_angle(gray)

            if abs(angle) < 0.5:
                return img

            h, w = img.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)

            # Calculate rotated bounding box
            cos = np.abs(M[0, 0])
            sin = np.abs(M[0, 1])
            new_w = int((h * sin) + (w * cos))
            new_h = int((h * cos) + (w * sin))

            M[0, 2] += (new_w / 2) - center[0]
            M[1, 2] += (new_h / 2) - center[1]

            rotated = cv2.warpAffine(img, M, (new_w, new_h), borderValue=(255, 255, 255))
            return rotated
        except Exception as e:
            logger.warning(f"Deskew failed: {e}")
            return img

    def _equalize_lighting(self, img: np.ndarray) -> np.ndarray:
        """Lighting equalization (CLAHE)"""
        try:
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            lab_l, a, b = cv2.split(lab)

            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            lab_l = clahe.apply(lab_l)

            lab = cv2.merge([lab_l, a, b])
            return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        except Exception as e:
            logger.warning(f"Lighting equalization failed: {e}")
            return img

    def _denoise(self, img: np.ndarray) -> np.ndarray:
        """Denoise (lightweight version)"""
        try:
            return cv2.fastNlMeansDenoisingColored(img, None, 10, 10, 7, 21)
        except Exception as e:
            logger.warning(f"Denoise failed: {e}")
            return img

    def _adaptive_threshold(self, img: np.ndarray) -> np.ndarray:
        """Adaptive threshold binarization"""
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # Use adaptive Gaussian threshold, preserve more detail
            binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                          cv2.THRESH_BINARY, 11, 2)
            # Convert back to BGR
            return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
        except Exception as e:
            logger.warning(f"Adaptive threshold failed: {e}")
            return img

    def save_corrected_image(self, image: np.ndarray, output_path: str) -> str:
        """Save corrected image"""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(image).save(output_path, quality=95)
        return output_path
