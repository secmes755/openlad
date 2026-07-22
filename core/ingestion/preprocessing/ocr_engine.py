"""
V4 OCR Engine - Pluggable multi-engine architecture
Load models on demand, release VRAM after processing
Supports: PaddleOCR / Tesseract / Multimodal LLM (image description)
"""
import logging
import gc
import base64
import requests
from io import BytesIO
import numpy as np
from enum import Enum
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image

logger = logging.getLogger(__name__)


class QualityLevel(Enum):
    """Text quality level - V4 three-tier degradation decision"""
    PASS = "pass"           # Pass → use direct extraction
    SUSPICIOUS = "suspicious"  # Suspicious → keep both texts, mark for confirmation
    FAIL = "fail"           # Fail → force OCR


# Try importing optional dependencies
try:
    from paddleocr import PaddleOCR
    HAS_PADDLEOCR = True
except ImportError:
    HAS_PADDLEOCR = False

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False


class OCRResult:
    """OCR recognition result"""
    def __init__(self, text: str, confidence: float = 0.0,
                 bbox: List[int] = None, page_num: int = 1):
        self.text = text
        self.confidence = confidence
        self.bbox = bbox or [0, 0, 0, 0]  # [x1, y1, x2, y2]
        self.page_num = page_num

    def to_dict(self) -> Dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "bbox": self.bbox,
            "page_num": self.page_num
        }


class OCREngine:
    """
    OCR Engine - Load model on demand
    VRAM management strategy:
    - Engine does not persist; initialized each time used
    - Release model reference after processing
    - Share the same engine instance during batch processing
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.engine_name = self.config.get("engine", "auto")
        self.language = self.config.get("language", "zh_en")
        self.min_confidence = self.config.get("min_confidence", 0.6)
        self.fallback_engine = self.config.get("fallback_engine", "tesseract")

        # Engine instance (initialized on demand)
        self._paddle_ocr = None
        self._tesseract_lang = None

    def _get_paddle_ocr(self) -> Optional[Any]:
        """Get or create PaddleOCR instance"""
        if not HAS_PADDLEOCR:
            return None

        if self._paddle_ocr is None:
            # Language mapping
            lang_map = {
                "zh": "ch",
                "en": "en",
                "zh_en": "ch",
                "ja": "japan",
                "ko": "korean"
            }
            paddle_lang = lang_map.get(self.language, "ch")

            logger.info(f"Initializing PaddleOCR (lang={paddle_lang})...")
            try:
                # FIX: PaddleOCR new version parameter changes
                # use_gpu deprecated → use device
                # show_log deprecated → removed
                self._paddle_ocr = PaddleOCR(
                    use_angle_cls=True,
                    lang=paddle_lang,
                    device='cpu'  # CPU mode, save VRAM
                )
                logger.info("PaddleOCR initialized (CPU mode)")
            except Exception as e:
                logger.error(f"PaddleOCR init failed: {e}")
                return None

        return self._paddle_ocr

    def _get_tesseract_lang(self) -> str:
        """Get Tesseract language parameter"""
        if self._tesseract_lang is None:
            lang_map = {
                "zh": "chi_sim",
                "en": "eng",
                "zh_en": "chi_sim+eng",
                "ja": "jpn",
                "ko": "kor"
            }
            self._tesseract_lang = lang_map.get(self.language, "chi_sim+eng")
        return self._tesseract_lang

    def recognize(self, image_path: str, page_num: int = 1) -> Tuple[str, List[OCRResult], Dict[str, Any]]:
        """
        Recognize text in an image

        Args:
            image_path: Path to the image
            page_num: Page number

        Returns:
            full_text: Merged text
            results: List of OCRResult (with position and confidence)
            metadata: Recognition metadata
        """
        if not Path(image_path).exists():
            return "", [], {"error": "File not found"}

        # Determine which engine to use
        engine = self._select_engine()
        logger.info(f"Using OCR engine: {engine} for {Path(image_path).name}")

        if engine == "paddleocr":
            return self._recognize_paddleocr(image_path, page_num)
        elif engine == "tesseract":
            return self._recognize_tesseract(image_path, page_num)
        elif engine == "vlm":
            return self._recognize_vlm(image_path, page_num)
        else:
            return "", [], {"error": "No OCR engine available"}

    def _select_engine(self) -> str:
        """Select OCR engine - V4: added VLM fallback"""
        if self.engine_name == "auto":
            # Prefer PaddleOCR for Chinese-heavy, otherwise Tesseract, finally VLM
            if self.language in ("zh", "zh_en", "ja", "ko"):
                if HAS_PADDLEOCR:
                    return "paddleocr"
                elif HAS_TESSERACT:
                    return "tesseract"
                else:
                    return "vlm"
            else:
                if HAS_TESSERACT:
                    return "tesseract"
                elif HAS_PADDLEOCR:
                    return "paddleocr"
                else:
                    return "vlm"
        elif self.engine_name == "paddleocr" and HAS_PADDLEOCR:
            return "paddleocr"
        elif self.engine_name == "tesseract" and HAS_TESSERACT:
            return "tesseract"
        elif self.engine_name == "vlm":
            return "vlm"

        # Fallback: try all available engines
        if HAS_PADDLEOCR:
            return "paddleocr"
        elif HAS_TESSERACT:
            return "tesseract"
        else:
            return "vlm"

    def _recognize_paddleocr(self, image_path: str, page_num: int) -> Tuple[str, List[OCRResult], Dict]:
        """Recognize using PaddleOCR - V4: added confidence filtering"""
        ocr = self._get_paddle_ocr()
        if ocr is None:
            return "", [], {"error": "PaddleOCR not available"}

        try:
            result = ocr.ocr(str(image_path), cls=True)

            ocr_results = []
            all_texts = []
            filtered_texts = []
            total_confidence = 0.0
            filtered_confidence = 0.0
            count = 0
            filtered_count = 0

            if result and len(result) > 0 and result[0]:
                for line in result[0]:
                    if line:
                        bbox = line[0]
                        text = line[1][0]
                        confidence = line[1][1]

                        # bbox format: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
                        x_coords = [p[0] for p in bbox]
                        y_coords = [p[1] for p in bbox]
                        simple_bbox = [min(x_coords), min(y_coords), max(x_coords), max(y_coords)]

                        ocr_result = OCRResult(
                            text=text,
                            confidence=confidence,
                            bbox=simple_bbox,
                            page_num=page_num
                        )
                        ocr_results.append(ocr_result)
                        all_texts.append(text)
                        total_confidence += confidence
                        count += 1

                        # V4: confidence filtering
                        if confidence >= self.min_confidence:
                            filtered_texts.append(text)
                            filtered_confidence += confidence
                            filtered_count += 1
                        else:
                            logger.debug(f"PaddleOCR dropped low-confidence block: confidence={confidence:.3f} < {self.min_confidence}, text='{text[:30]}...'")

            # Use filtered text
            full_text = "\n".join(filtered_texts) if filtered_texts else "\n".join(all_texts)
            avg_confidence = (filtered_confidence / filtered_count if filtered_count > 0
                           else total_confidence / count if count > 0 else 0.0)

            metadata = {
                "engine": "paddleocr",
                "total_blocks": count,
                "filtered_blocks": filtered_count,
                "dropped_blocks": count - filtered_count,
                "avg_confidence": avg_confidence,
                "min_confidence_threshold": self.min_confidence,
                "language": self.language,
                "page_num": page_num
            }

            return full_text, ocr_results, metadata

        except Exception as e:
            logger.error(f"PaddleOCR failed: {e}")
            # Try fallback
            if self.fallback_engine == "tesseract" and HAS_TESSERACT:
                return self._recognize_tesseract(image_path, page_num)
            return "", [], {"error": str(e)}

    def _recognize_tesseract(self, image_path: str, page_num: int) -> Tuple[str, List[OCRResult], Dict]:
        """
        Recognize using Tesseract - V4: added confidence filtering
        """
        if not HAS_TESSERACT:
            return "", [], {"error": "Tesseract not available"}

        try:
            img = Image.open(image_path)
            lang = self._get_tesseract_lang()

            # Get precise OCR data (including position info)
            data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)

            ocr_results = []
            all_texts = []
            filtered_texts = []
            total_confidence = 0.0
            filtered_confidence = 0.0
            count = 0
            filtered_count = 0

            n_boxes = len(data['text'])
            for i in range(n_boxes):
                text = data['text'][i].strip()
                conf = int(data['conf'][i])

                if text and conf > 0:
                    x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
                    bbox = [x, y, x + w, y + h]
                    confidence = conf / 100.0

                    ocr_result = OCRResult(
                        text=text,
                        confidence=confidence,
                        bbox=bbox,
                        page_num=page_num
                    )
                    ocr_results.append(ocr_result)
                    all_texts.append(text)
                    total_confidence += confidence
                    count += 1

                    # V4: confidence filtering
                    if confidence >= self.min_confidence:
                        filtered_texts.append(text)
                        filtered_confidence += confidence
                        filtered_count += 1
                    else:
                        logger.debug(f"Tesseract dropped low-confidence block: confidence={confidence:.3f} < {self.min_confidence}, text='{text[:30]}...'")

            # Use filtered text
            full_text = "\n".join(filtered_texts) if filtered_texts else "\n".join(all_texts)
            avg_confidence = (filtered_confidence / filtered_count if filtered_count > 0
                           else total_confidence / count if count > 0 else 0.0)

            metadata = {
                "engine": "tesseract",
                "total_blocks": count,
                "filtered_blocks": filtered_count,
                "dropped_blocks": count - filtered_count,
                "avg_confidence": avg_confidence,
                "min_confidence_threshold": self.min_confidence,
                "language": self.language,
                "page_num": page_num
            }

            return full_text, ocr_results, metadata

        except Exception as e:
            logger.error(f"Tesseract failed: {e}")
            return "", [], {"error": str(e)}

    def _recognize_vlm(self, image_path: str, page_num: int) -> Tuple[str, List[OCRResult], Dict]:
        """Recognize using VLM (9B multimodal model) - V4: fallback when PaddleOCR/Tesseract unavailable"""
        try:
            # Load and encode image
            img = Image.open(image_path).convert('RGB')
            buffer = BytesIO()
            img.save(buffer, format='PNG')
            base64_image = base64.b64encode(buffer.getvalue()).decode('utf-8')

            # Build vision API request
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract all text from this image. Preserve the layout and formatting as much as possible. Output only the extracted text, no explanations."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                ]
            }]

            # Call VLM API via configured endpoint
            from ....config import settings
            vlm_url = f"{settings.CHART_VLM_BASE_URL}/chat/completions"
            response = requests.post(
                vlm_url,
                json={
                    'model': settings.CHART_VLM_MODEL_NAME,
                    'messages': messages,
                    'max_tokens': 2000,
                    'temperature': 0.0
                },
                timeout=60
            )

            if response.status_code != 200:
                logger.error(f"VLM OCR failed: HTTP {response.status_code}")
                return "", [], {"error": f"VLM API error: {response.status_code}"}

            result = response.json()
            extracted_text = result['choices'][0]['message']['content'].strip()

            # Create OCRResult
            ocr_result = OCRResult(
                text=extracted_text,
                confidence=0.85,  # VLM doesn't provide confidence, use reasonable default
                bbox=[0, 0, img.width, img.height],
                page_num=page_num
            )

            logger.info(f"VLM OCR page {page_num}: extracted {len(extracted_text)} chars")

            return extracted_text, [ocr_result], {
                "engine": "vlm",
                "total_blocks": 1,
                "filtered_blocks": 1,
                "avg_confidence": 0.85,
                "language": self.language,
                "page_num": page_num
            }

        except Exception as e:
            logger.error(f"VLM OCR failed: {e}")
            return "", [], {"error": f"VLM OCR failed: {str(e)}"}

    def release(self):
        """Release OCR engine, clear VRAM"""
        if self._paddle_ocr is not None:
            logger.info("Releasing PaddleOCR engine...")
            self._paddle_ocr = None
            gc.collect()
            # Try to clear CUDA cache
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.info("CUDA cache cleared")
            except ImportError:
                pass

    def __del__(self):
        self.release()


class TextQualityChecker:
    """
    V4 Text quality checker
    Solves quality issues with hidden text layers in searchable PDFs
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.garbled_threshold = self.config.get("garbled_threshold", 0.05)
        self.min_dictionary_hit_rate = self.config.get("min_dictionary_hit_rate", 0.3)
        self.enable_ocr_fallback = self.config.get("enable_ocr_fallback", True)

        # Common Chinese word list (generic, not industry-specific)
        self._cn_common_words = set([
            "的", "是", "在", "和", "了", "与", "有", "被", "为", "以",
            "及", "对", "或", "将", "从", "到", "也", "而", "其", "之",
            "中", "上", "下", "内", "外", "时", "后", "前", "间",
        ])
        
        # Industry-specific terms injected from industry pack, not hardcoded in core
        self._industry_terms = set()

    def _load_industry_terms(self, industry_pack=None):
        """Load terminology word list from industry pack"""
        if industry_pack and hasattr(industry_pack, 'ingestion'):
            terms = getattr(industry_pack.ingestion, 'ocr_quality_terms', [])
            if terms:
                self._industry_terms = set(terms)
                return
        # Default empty, no industry terms hardcoded
        self._industry_terms = set()

    def set_industry_pack(self, industry_pack):
        """Set industry pack, load industry-specific terms"""
        self._load_industry_terms(industry_pack)

    def check(self, text: str) -> Dict[str, Any]:
        """
        Check text quality - V4 three-tier degradation decision

        Returns:
            {
                "quality_score": float (0-1),
                "quality_level": QualityLevel,
                "is_garbled": bool,
                "garbled_ratio": float,
                "dict_hit_rate": float,
                "needs_ocr": bool,
                "details": dict
            }
        """
        if not text or len(text.strip()) < 10:
            return {
                "quality_score": 0.0,
                "quality_level": QualityLevel.FAIL,
                "is_garbled": True,
                "garbled_ratio": 1.0,
                "dict_hit_rate": 0.0,
                "needs_ocr": True,
                "details": {"reason": "text_too_short"}
            }

        text = text.strip()
        details = {}

        # 1. Character level: detect garbled character ratio
        garbled_ratio = self._detect_garbled(text)
        details["garbled_ratio"] = garbled_ratio
        is_garbled = garbled_ratio > self.garbled_threshold

        # 2. Word level: dictionary hit rate
        dict_hit_rate = self._check_dictionary_hits(text)
        details["dict_hit_rate"] = dict_hit_rate

        # 3. Structure level: check paragraph integrity
        paragraph_score = self._check_paragraph_integrity(text)
        details["paragraph_score"] = paragraph_score

        # Composite score
        quality_score = (
            (1.0 - garbled_ratio) * 0.4 +
            dict_hit_rate * 0.3 +
            paragraph_score * 0.3
        )

        # V4: three-tier degradation decision
        # Pass: score >= 0.7 and no serious issues
        # Suspicious: score >= 0.4 and < 0.7, or minor issues
        # Fail: score < 0.4 or serious issues
        if quality_score >= 0.7 and not is_garbled and dict_hit_rate >= 0.3:
            quality_level = QualityLevel.PASS
            needs_ocr = False
        elif quality_score >= 0.4 and not is_garbled:
            quality_level = QualityLevel.SUSPICIOUS
            needs_ocr = False  # Suspicious does not force OCR, but marks for confirmation
        else:
            quality_level = QualityLevel.FAIL
            needs_ocr = self.enable_ocr_fallback

        details["quality_level"] = quality_level.value

        return {
            "quality_score": quality_score,
            "quality_level": quality_level,
            "is_garbled": is_garbled,
            "garbled_ratio": garbled_ratio,
            "dict_hit_rate": dict_hit_rate,
            "needs_ocr": needs_ocr,
            "details": details
        }

    def compare_and_decide(self, direct_text: str, ocr_text: str) -> Tuple[str, str, Dict]:
        """
        Compare directly extracted text and OCR text, based on V4 three-tier degradation decision

        Returns:
            (chosen_text, text_source, metadata)
            text_source: "direct_extract" | "ocr" | "both_suspicious"
        """
        direct_quality = self.check(direct_text)
        ocr_quality = self.check(ocr_text)

        direct_level = direct_quality["quality_level"]
        ocr_level = ocr_quality["quality_level"]

        # V4: three-tier degradation decision
        if direct_level == QualityLevel.PASS:
            # Pass → use direct extraction
            return direct_text, "direct_extract", {
                "direct_quality": direct_quality,
                "ocr_quality": ocr_quality,
                "winner": "direct",
                "reason": "direct_pass"
            }
        elif direct_level == QualityLevel.SUSPICIOUS:
            # Suspicious → keep both texts, mark for confirmation
            # Choose the higher-quality one as primary, but retain both
            if direct_quality["quality_score"] >= ocr_quality["quality_score"]:
                primary_text = direct_text
                secondary_text = ocr_text
                winner = "direct"
            else:
                primary_text = ocr_text
                secondary_text = direct_text
                winner = "ocr"

            # Merge both texts, mark for confirmation
            combined_text = f"[AUTO_EXTRACTED]\n{primary_text}\n\n[OCR_FALLBACK - needs confirmation]\n{secondary_text}"
            return combined_text, "both_suspicious", {
                "direct_quality": direct_quality,
                "ocr_quality": ocr_quality,
                "winner": winner,
                "reason": "suspicious_both_kept",
                "needs_confirmation": True
            }
        else:
            # Fail → force OCR (if OCR quality is better)
            if ocr_level != QualityLevel.FAIL and ocr_quality["quality_score"] > direct_quality["quality_score"]:
                return ocr_text, "ocr", {
                    "direct_quality": direct_quality,
                    "ocr_quality": ocr_quality,
                    "winner": "ocr",
                    "reason": "direct_fail_ocr_better"
                }
            else:
                # OCR is also poor, but still use OCR as last resort
                return ocr_text if ocr_text else direct_text, "ocr_fallback", {
                    "direct_quality": direct_quality,
                    "ocr_quality": ocr_quality,
                    "winner": "ocr_fallback",
                    "reason": "both_poor"
                }

    def _detect_garbled(self, text: str) -> float:
        """Detect garbled character ratio"""
        if not text:
            return 1.0

        garbled_count = 0
        total_chars = 0

        for char in text:
            # Skip whitespace
            if char.isspace():
                continue
            total_chars += 1

            # Check if it's a valid Unicode character
            code = ord(char)

            # Detect replacement characters (e.g. □, , ■ etc.)
            if code in (0x25a1, 0xfffd, 0x25a0, 0x201a, 0x0192):
                garbled_count += 1
                continue

            # Detect control characters (except common newline, tab, and PDF format control chars)
            # FIX: PDF text layer often contains \x01-\x08 control chars for formatting, not garbled text
            if code < 32 and code not in (9, 10, 13, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15):
                garbled_count += 1
                continue

            # Detect invalid high Unicode
            if code > 0x10FFFF:
                garbled_count += 1
                continue

        return garbled_count / total_chars if total_chars > 0 else 1.0

    def _check_dictionary_hits(self, text: str) -> float:
        """Check dictionary hit rate"""
        if not text:
            return 0.0

        # Extract 2-3 character Chinese word groups
        import re
        words = re.findall(r'[\u4e00-\u9fff]{2,3}', text)

        if not words:
            # If no Chinese, check English words
            en_words = re.findall(r'[a-zA-Z]{2,}', text)
            if en_words:
                # Simple check: assume English words are valid
                return min(len(en_words) / 10, 1.0)
            return 0.0

        hits = sum(1 for w in words if w in self._cn_common_words)
        # Also check industry-specific terms (injected from industry pack)
        if self._industry_terms:
            hits += sum(1 for w in words if w in self._industry_terms)

        return min(hits / len(words), 1.0) if words else 0.0

    def _check_paragraph_integrity(self, text: str) -> float:
        """Check paragraph integrity"""
        if not text:
            return 0.0

        lines = text.split('\n')
        if len(lines) < 2:
            return 0.5  # Too short, cannot determine

        scores = []

        # Check if line length distribution is reasonable
        line_lengths = [len(line.strip()) for line in lines if line.strip()]
        if line_lengths:
            avg_len = sum(line_lengths) / len(line_lengths)
            # Most lines should have reasonable length
            reasonable_lines = sum(1 for l in line_lengths if 5 < l < 200)
            scores.append(reasonable_lines / len(line_lengths))

        # Check for many truncated words (very short line followed by very long line)
        if len(line_lengths) >= 3:
            truncated = 0
            for i in range(len(line_lengths) - 1):
                if line_lengths[i] < 5 and line_lengths[i+1] > 50:
                    truncated += 1
            scores.append(1.0 - min(truncated / len(line_lengths), 1.0))

        return sum(scores) / len(scores) if scores else 0.5
