"""
VLM-based Page Classifier — uses 9B VLM at low resolution to classify a page into one of
CHART / IMAGE / TEXT / BLANK.

Design principles:
- Use 72 DPI low-resolution images (~0.4s/page) for classification
- Fast blank-page detection is done via image histograms before any VLM call
- No hardcoded thresholds for content classes — relies entirely on VLM understanding
- Singleton pattern, reuses ModelClient connection
"""
import base64
import logging
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)

# Classification Prompt: minimal constraints, minimal output tokens.
# Scanned pages with text-as-image are intentionally classified as IMAGE so that
# the ingestion pipeline can ask a VLM to transcribe them.
CLASSIFY_PROMPT = (
    "You are analyzing a document page. Answer with EXACTLY ONE word:\n"
    "- CHART if this page contains any diagram, block diagram, flowchart, "
    "table, graph, pinout, or figure\n"
    "- IMAGE if this page contains a photograph, screenshot, scanned page, "
    "or other non-text visual content\n"
    "- TEXT if this page contains only selectable/typed text (paragraphs, lists, headings, "
    "table of contents)\n"
    "- BLANK if this page is empty or nearly empty\n\n"
    "Answer:"
)

CLASSIFY_MAX_TOKENS = 10
CLASSIFY_TEMPERATURE = 0.0


def is_blank_image(page_image, blank_threshold: float = 0.005,
                   white_threshold: int = 250) -> bool:
    """
    Fast blank-page detection using the grayscale histogram.

    A page is considered blank if the ratio of non-white pixels is below
    ``blank_threshold``. This avoids calling a VLM on empty or nearly empty pages.

    Args:
        page_image: PIL Image object
        blank_threshold: maximum ratio of non-white pixels for a blank page
        white_threshold: grayscale value above which a pixel is treated as white

    Returns:
        True if the page looks blank, False otherwise.
    """
    if page_image is None:
        return True
    try:
        gray = page_image.convert("L")
        hist = gray.histogram()
        total = gray.width * gray.height
        if total == 0:
            return True
        non_white = sum(hist[:white_threshold])
        return (non_white / total) < blank_threshold
    except Exception as e:
        logger.debug(f"is_blank_image failed: {e}")
        return False


class PageClassifier:
    """VLM page classifier — determines if a page is a chart, image, text, or blank"""

    _instance: Optional["PageClassifier"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._stats = {"total": 0, "chart": 0, "image": 0, "text": 0, "blank": 0}

    def classify(self, page_image, page_num: int = 0) -> str:
        """
        Classify page: returns "CHART", "IMAGE", "TEXT", or "BLANK".

        The caller is expected to have already checked blank images via
        ``is_blank_image()`` to avoid wasting a VLM call. This method still
        accepts BLANK as a valid response so it degrades safely.

        Args:
            page_image: PIL Image object (recommended 72 DPI)
            page_num: Page number (for logging)

        Returns:
            One of "CHART", "IMAGE", "TEXT", "BLANK"
        """
        if is_blank_image(page_image):
            self._stats["total"] += 1
            self._stats["blank"] += 1
            return "BLANK"

        try:
            from ...models import get_model_client

            # Encode PIL Image as base64 PNG
            buffer = BytesIO()
            if page_image.mode in ('RGBA', 'P', 'LA'):
                page_image = page_image.convert('RGB')
            page_image.save(buffer, format='PNG')

            base64_image = base64.b64encode(buffer.getvalue()).decode('utf-8')

            # Build OpenAI-compatible vision API request
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": CLASSIFY_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{base64_image}"
                    }}
                ]
            }]

            client = get_model_client()
            result = client._chat_completion(
                messages,
                max_tokens=CLASSIFY_MAX_TOKENS,
                temperature=CLASSIFY_TEMPERATURE
            )

            result_upper = result.strip().upper()
            if "CHART" in result_upper:
                label = "CHART"
            elif "IMAGE" in result_upper:
                label = "IMAGE"
            elif "TEXT" in result_upper:
                label = "TEXT"
            elif "BLANK" in result_upper:
                label = "BLANK"
            else:
                logger.warning(
                    f"PageClassifier p{page_num}: unusual response "
                    f"'{result[:80]}' → fallback TEXT"
                )
                label = "TEXT"

            self._stats["total"] += 1
            self._stats[label.lower()] += 1

            return label

        except Exception as e:
            logger.warning(f"PageClassifier p{page_num}: classification failed {e}")
            self._stats["total"] += 1
            self._stats["text"] += 1
            return "TEXT"

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def reset_stats(self):
        self._stats = {"total": 0, "chart": 0, "image": 0, "text": 0, "blank": 0}
