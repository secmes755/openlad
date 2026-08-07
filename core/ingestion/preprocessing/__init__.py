"""
V4 Document Preprocessing Pipeline
Integrates image correction, OCR, and text quality validation
"""
import logging
from pathlib import Path

import numpy as np
from PIL import Image

from ...config import settings
from .image_corrector import ImageCorrector
from .ocr_engine import OCREngine, TextQualityChecker

logger = logging.getLogger(__name__)


class PagePreprocessResult:
    """Page preprocessing result"""
    def __init__(self):
        self.page_num: int = 0
        self.raw_text: str = ""
        self.text_source: str = "direct_extract"  # direct_extract / ocr / mixed
        self.ocr_confidence: float = 0.0
        self.page_image_path: str = ""  # Original/corrected image path
        self.ocr_results: list[dict] = []  # OCR results (with bbox)
        self.quality_metrics: dict = {}
        self.correction_metrics: dict = {}

    def to_dict(self) -> dict:
        return {
            "page_num": self.page_num,
            "raw_text": self.raw_text,
            "text_source": self.text_source,
            "ocr_confidence": self.ocr_confidence,
            "page_image_path": self.page_image_path,
            "ocr_results": self.ocr_results,
            "quality_metrics": self.quality_metrics,
            "correction_metrics": self.correction_metrics
        }


class DocumentPreprocessor:
    """
    V4 Document Preprocessing Pipeline
    Processing flow:
    1. Detect whether each page has a text layer
    2. No text layer → image correction → OCR → text quality validation
    3. Has text layer → text quality check → decide whether OCR fallback is needed
    4. Output: trusted text per page + text source label
    """

    def __init__(self, images_dir: str = None):
        self.corrector = ImageCorrector()
        self.ocr_engine = OCREngine(settings.OCR_CONFIG)
        self.quality_checker = TextQualityChecker(settings.TEXT_QUALITY_CONFIG)
        self.images_dir = Path(images_dir) if images_dir else settings.IMAGES_DIR

    def preprocess_pdf_page(self, page_image: Image.Image, page_num: int,
                           direct_text: str = "", force_ocr: bool = False,
                           doc_id: str = "") -> PagePreprocessResult:
        """
        Process a single PDF page

        Args:
            page_image: PIL Image converted from PDF page
            page_num: Page number
            direct_text: Directly extracted text (if any)
            force_ocr: Whether to force OCR usage
            doc_id: Document ID (for saving images)

        Returns:
            PagePreprocessResult
        """
        result = PagePreprocessResult()
        result.page_num = page_num

        # Save page image
        image_filename = f"{doc_id}_p{page_num}.png" if doc_id else f"page_{page_num}.png"
        image_path = self.images_dir / image_filename
        page_image.save(image_path, "PNG")
        result.page_image_path = str(image_path)

        try:
            # Determine processing path
            if force_ocr or not direct_text:
                # Force OCR or no direct text
                logger.info(f"Page {page_num}: Using OCR pipeline")
                result = self._ocr_pipeline(page_image, page_num, result)
            else:
                # Has direct extracted text, check quality first
                quality = self.quality_checker.check(direct_text)
                result.quality_metrics = quality

                if quality["needs_ocr"]:
                    logger.info(f"Page {page_num}: Direct text quality low ({quality['quality_score']:.2f}), "
                               f"running OCR fallback")
                    # Run OCR and compare
                    ocr_result = self._ocr_pipeline(page_image, page_num, PagePreprocessResult())
                    chosen_text, text_source, meta = self.quality_checker.compare_and_decide(
                        direct_text, ocr_result.raw_text
                    )
                    result.raw_text = chosen_text
                    result.text_source = text_source
                    result.ocr_confidence = ocr_result.ocr_confidence
                    result.ocr_results = ocr_result.ocr_results
                    result.quality_metrics["comparison"] = meta
                else:
                    logger.info(f"Page {page_num}: Direct text quality OK ({quality['quality_score']:.2f})")
                    result.raw_text = direct_text
                    result.text_source = "direct_extract"

            return result

        except Exception as e:
            logger.error(f"Preprocess page {page_num} failed: {e}")
            # Fallback: use directly extracted text (if available)
            result.raw_text = direct_text or ""
            result.text_source = "direct_extract"
            return result

        finally:
            # Release OCR engine when no longer needed
            if page_num % 5 == 0:  # Release every 5 pages
                self.ocr_engine.release()

    def _ocr_pipeline(self, page_image: Image.Image, page_num: int,
                     result: PagePreprocessResult) -> PagePreprocessResult:
        """
        OCR processing pipeline
        """
        # 1. Image correction
        temp_path = None  # Ensure scope safety, avoids NameError when enable_deskew=False
        if settings.OCR_CONFIG.get("enable_deskew", True):
            # Convert PIL Image to numpy array for OpenCV
            np.array(page_image.convert('RGB'))

            # Save temp file for corrector
            temp_path = self.images_dir / f"temp_p{page_num}.png"
            page_image.save(temp_path, "PNG")

            corrected_array, metrics = self.corrector.correct(str(temp_path))
            result.correction_metrics = metrics

            # Save corrected image
            corrected_image = Image.fromarray(corrected_array)
        else:
            corrected_image = page_image

        # 2. Run OCR
        # Save corrected image for OCR
        ocr_input_path = self.images_dir / f"ocr_p{page_num}.png"
        corrected_image.save(ocr_input_path, "PNG")

        full_text, ocr_results, metadata = self.ocr_engine.recognize(
            str(ocr_input_path), page_num=page_num
        )

        result.raw_text = full_text
        result.text_source = "ocr"
        result.ocr_confidence = metadata.get("avg_confidence", 0.0)
        result.ocr_results = [r.to_dict() for r in ocr_results]

        # Clean up temporary files
        try:
            if temp_path and temp_path.exists():
                temp_path.unlink()
            if ocr_input_path.exists():
                ocr_input_path.unlink()
        except Exception:
            pass

        return result

    def preprocess_image_file(self, image_path: str, doc_id: str = "") -> PagePreprocessResult:
        """
        Process a single image file (jpg/png, etc.)
        """
        img = Image.open(image_path).convert('RGB')
        return self.preprocess_pdf_page(img, page_num=1, direct_text="", force_ocr=True, doc_id=doc_id)

    def release(self):
        """Release all resources"""
        self.ocr_engine.release()
        logger.info("DocumentPreprocessor resources released")

    def __del__(self):
        self.release()


# Exports
from .ocr_engine import OCRResult

__all__ = [
    "DocumentPreprocessor",
    "PagePreprocessResult",
    "ImageCorrector",
    "OCREngine",
    "OCRResult",
    "TextQualityChecker"
]
