"""
VLM-based Page Classifier — uses 9B VLM at low resolution to determine if a page contains charts

Design principles:
- Use 72 DPI low-resolution images (~0.4s/page) for binary classification (CHART / TEXT)
- Cache classification results in content_dict to avoid redundant calls
- No hardcoded thresholds — relies entirely on VLM understanding
- Singleton pattern, reuses ModelClient connection
"""
import base64
import logging
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)

# Classification Prompt: minimal constraints, minimal output tokens
CLASSIFY_PROMPT = (
    "You are analyzing a document page. Answer with EXACTLY ONE word:\n"
    "- CHART if this page contains any diagram, block diagram, flowchart, "
    "table, graph, pinout, or figure\n"
    "- TEXT if this page contains only text (paragraphs, lists, headings, "
    "table of contents)\n\n"
    "Answer:"
)

CLASSIFY_MAX_TOKENS = 10
CLASSIFY_TEMPERATURE = 0.0


class PageClassifier:
    """VLM page classifier — determines if a page contains charts/diagrams"""

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
        self._stats = {"total": 0, "chart": 0, "text": 0}

    def classify(self, page_image, page_num: int = 0) -> str:
        """
        Classify page: returns "CHART" or "TEXT"

        Args:
            page_image: PIL Image object (recommended 72 DPI)
            page_num: Page number (for logging)

        Returns:
            "CHART" or "TEXT"
        """
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
            elif "TEXT" in result_upper:
                label = "TEXT"
            else:
                logger.warning(
                    f"PageClassifier p{page_num}: unusual response "
                    f"'{result[:80]}' → fallback TEXT"
                )
                label = "TEXT"

            self._stats["total"] += 1
            if label == "CHART":
                self._stats["chart"] += 1
            else:
                self._stats["text"] += 1

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
        self._stats = {"total": 0, "chart": 0, "text": 0}
