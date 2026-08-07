"""
Layout Analysis Engine
Lightweight implementation based on heuristic rules + OCR bbox
Supports: column count detection, reading order restoration, element classification, figure-caption association

Can be replaced with deep learning models later (PP-DocLayoutV3/YOLOX-Layout)
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class LayoutElement:
    """Layout element"""
    order: int = 0
    element_type: str = "text"  # text/title/section-header/caption/footnote/
                               # page-header/page-footer/picture/table/formula/list-item
    content: Any = ""
    bbox: list[int] = field(default_factory=lambda: [0, 0, 0, 0])  # [x1, y1, x2, y2]
    children: list[int] = field(default_factory=list)
    caption_ref: str = ""
    refers_to: str = ""
    source: str = "direct_extract"  # direct_extract / ocr / vl_model

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "type": self.element_type,
            "content": self.content,
            "bbox": self.bbox,
            "children": self.children,
            "caption_ref": self.caption_ref,
            "refers_to": self.refers_to,
            "source": self.source
        }


@dataclass
class LayoutResult:
    """Layout analysis result"""
    page_num: int = 0
    page_type: str = "text_body"  # cover/toc/text_body/table_page/form/image_page/appendix/blank/other
    columns: int = 1
    primary_direction: str = "top_to_bottom"
    elements: list[LayoutElement] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "page_num": self.page_num,
            "page_type": self.page_type,
            "layout": {
                "columns": self.columns,
                "primary_direction": self.primary_direction
            },
            "reading_order": [e.to_dict() for e in self.elements],
            "metadata": self.metadata
        }


class LayoutAnalyzer:
    """
    Layout Analysis Engine
    Lightweight rule-based implementation
    """

    # Element types
    ELEMENT_TYPES = [
        "text", "title", "section-header", "caption", "footnote",
        "page-header", "page-footer", "picture", "table", "formula", "list-item"
    ]

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.detect_columns = self.config.get("detect_columns", True)
        self.restore_reading_order = self.config.get("restore_reading_order", True)
        self.element_types = self.config.get("element_types", self.ELEMENT_TYPES)

    def analyze(self, raw_text: str, page_num: int = 1,
                ocr_results: list[dict] = None,
                image_path: str = None) -> LayoutResult:
        """
        Analyze page layout

        Args:
            raw_text: Raw page text
            page_num: Page number
            ocr_results: OCR results (with bbox)
            image_path: Page image path

        Returns:
            LayoutResult
        """
        result = LayoutResult(page_num=page_num)

        # 1. Split text into blocks
        text_blocks = self._split_text_blocks(raw_text)

        # 2. Merge bbox info if OCR results are available
        if ocr_results:
            blocks_with_bbox = self._merge_ocr_blocks(text_blocks, ocr_results)
        else:
            blocks_with_bbox = self._estimate_bbox(text_blocks)

        # 3. Detect column count
        if self.detect_columns and len(blocks_with_bbox) > 3:
            result.columns = self._detect_columns(blocks_with_bbox)

        # 4. Classify elements
        classified_blocks = self._classify_elements(blocks_with_bbox)

        # 5. Restore reading order
        if self.restore_reading_order:
            ordered_elements = self._restore_reading_order(classified_blocks, result.columns)
        else:
            ordered_elements = classified_blocks

        # 6. Link captions with figures/tables
        ordered_elements = self._link_captions(ordered_elements)

        # 7. Detect page type
        result.page_type = self._detect_page_type(ordered_elements)

        result.elements = ordered_elements
        result.metadata = {
            "total_blocks": len(ordered_elements),
            "text_blocks": sum(1 for e in ordered_elements if e.element_type == "text"),
            "has_table": any(e.element_type == "table" for e in ordered_elements),
            "has_formula": any(e.element_type == "formula" for e in ordered_elements),
            "has_picture": any(e.element_type == "picture" for e in ordered_elements),
        }

        return result

    def _split_text_blocks(self, text: str) -> list[tuple[str, list[int]]]:
        """Split text into blocks (text, estimated bbox)"""
        blocks = []
        lines = text.split('\n')
        current_block = []
        y_pos = 0
        line_height = 20  # Estimated line height

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if current_block:
                    block_text = '\n'.join(current_block)
                    # Estimated bbox: [x, y, width, y+height]
                    max_width = max(len(ln) * 12 for ln in current_block)  # Estimate 12px per character
                    bbox = [0, y_pos - len(current_block) * line_height, max_width, y_pos]
                    blocks.append((block_text, bbox))
                    current_block = []
                y_pos += line_height
                continue

            current_block.append(stripped)
            y_pos += line_height

        if current_block:
            block_text = '\n'.join(current_block)
            max_width = max(len(ln) * 12 for ln in current_block)
            bbox = [0, y_pos - len(current_block) * line_height, max_width, y_pos]
            blocks.append((block_text, bbox))

        return blocks

    def _merge_ocr_blocks(self, text_blocks: list[tuple[str, list[int]]],
                         ocr_results: list[dict]) -> list[tuple[str, list[int], float]]:
        """Merge OCR bbox information"""
        blocks = []

        # Use OCR results as primary source
        for ocr in ocr_results:
            text = ocr.get("text", "")
            bbox = ocr.get("bbox", [0, 0, 0, 0])
            conf = ocr.get("confidence", 0.0)
            if text.strip():
                blocks.append((text, bbox, conf))

        # Supplement with directly extracted text blocks if OCR results are insufficient
        if len(blocks) < len(text_blocks):
            existing_texts = {b[0] for b in blocks}
            for tb, bbox in text_blocks:
                if tb not in existing_texts and len(tb) > 10:
                    blocks.append((tb, bbox, 0.5))

        return blocks

    def _estimate_bbox(self, text_blocks: list[tuple[str, list[int]]]) -> list[tuple[str, list[int], float]]:
        """Use estimated bbox"""
        return [(text, bbox, 0.5) for text, bbox in text_blocks]

    def _detect_columns(self, blocks: list[tuple[str, list[int], float]]) -> int:
        """Detect column count (based on x-coordinate clustering)"""
        if len(blocks) < 3:
            return 1

        # Get center x coordinate of each block
        centers = []
        for _, bbox, _ in blocks:
            x1, _, x2, _ = bbox
            center = (x1 + x2) / 2
            centers.append(center)

        if not centers:
            return 1

        # K-means clustering (k=1 or k=2)
        centers = np.array(centers).reshape(-1, 1)

        # Detect if there is a clear bimodal distribution (prefer scipy, fall back to simple rules)
        try:
            from scipy import stats
            kde = stats.gaussian_kde(centers.flatten())
            x_range = np.linspace(min(centers), max(centers), 100)
            density = kde(x_range)

            # Check peaks in density function
            peaks = []
            for i in range(1, len(density) - 1):
                if density[i] > density[i-1] and density[i] > density[i+1]:
                    peaks.append(x_range[i])

            # If there are two clearly separated peaks, consider it dual-column
            if len(peaks) >= 2:
                peak_dist = abs(peaks[0] - peaks[1])
                total_width = max(centers) - min(centers)
                if peak_dist > total_width * 0.25:
                    return 2

            # Check for three columns
            if len(peaks) >= 3:
                return 3

        except ImportError:
            logger.debug("scipy not available, using simple column detection")
            # Simple rule: split x coordinates in half and see how many blocks in each half
            total_width = max(centers) - min(centers)
            mid = min(centers) + total_width / 2
            left_count = sum(1 for c in centers if c < mid)
            right_count = sum(1 for c in centers if c >= mid)
            # If both sides have a fair number of blocks, consider it dual-column
            if left_count >= 2 and right_count >= 2 and abs(left_count - right_count) <= max(left_count, right_count) * 0.5:
                return 2
        except Exception as e:
            logger.debug(f"Column detection failed: {e}")

        return 1

    def _classify_elements(self, blocks: list[tuple[str, list[int], float]]) -> list[LayoutElement]:
        """Classify text blocks into element types"""
        elements = []

        for idx, (text, bbox, conf) in enumerate(blocks):
            text = text.strip()
            if not text:
                continue

            elem = LayoutElement()
            elem.bbox = bbox
            elem.source = "ocr" if conf > 0 else "direct_extract"

            # Determine element type
            elem.element_type = self._detect_element_type(text, bbox, idx, blocks)
            elem.content = text
            elem.order = idx + 1

            elements.append(elem)

        return elements

    def _detect_element_type(self, text: str, bbox: list[int],
                            idx: int, all_blocks: list) -> str:
        """Detect element type for a single text block"""
        text.lower()
        text_len = len(text)

        # 1. Title
        if text_len < 50:
            # Short text, check if it's a title
            if self._is_likely_title(text):
                # Distinguish between level-1 and level-2 titles
                if text_len < 20 and not text.endswith('：') and not text.endswith(':'):
                    return "title"
                else:
                    return "section-header"

        # 3. Figure/table caption
        caption_patterns = [
            r'^图\s*\d+', r'^Fig\.?\s*\d+', r'^Figure\s*\d+',
            r'^表\s*\d+', r'^Table\s*\d+', r'^TABLE\s*\d+',
            r'^[图表]\s*[一-九〇\d]+'
        ]
        for pattern in caption_patterns:
            if re.match(pattern, text, re.IGNORECASE):
                return "caption"

        # 4. Footnote
        if text_len < 200 and text.startswith(('*', '※', '①', '[', '(')):
            if any(kw in text for kw in ['注', 'note', '参考', 'ref']):
                return "footnote"

        # 5. List item
        if re.match(r'^[\s]*[•\-\*\d+\.\u2460-\u24ff][\s]', text):
            return "list-item"

        # 6. Table (check for table features)
        if '|' in text or '｜' in text or '\t' in text:
            lines = text.split('\n')
            if len(lines) >= 2 and all('|' in ln or '\t' in ln for ln in lines[:2]):
                return "table"

        # 7. Formula (check for math symbols)
        formula_indicators = ['=', '+', '-', '×', '÷', 'Σ', 'Π', '∂', '∫',
                             '√', '≠', '≤', '≥', '∞', 'α', 'β', 'γ',
                             '^', '_', '{', '}', '\\frac', '\\sum', '\\int']
        if any(ind in text for ind in formula_indicators):
            # Check formula feature strength
            formula_chars = sum(1 for c in text if c in formula_indicators)
            if formula_chars / max(len(text), 1) > 0.1:
                return "formula"

        # Default to body text
        return "text"

    def _is_likely_title(self, text: str) -> bool:
        """Check if likely a title"""
        # Title features
        title_patterns = [
            r'^[\d\.\s]+',  # Numbered prefix
            r'^[\d\.]+\s+\S',  # Number + text
        ]

        for pattern in title_patterns:
            if re.match(pattern, text):
                return True

        # Short text without punctuation (likely a title)
        if len(text) < 30 and not any(c in text for c in '，。；：'):
            return True

        return False

    def _restore_reading_order(self, elements: list[LayoutElement],
                               columns: int) -> list[LayoutElement]:
        """Restore reading order"""
        if len(elements) <= 1:
            return elements

        if columns == 1:
            # Single column: sort by y coordinate
            sorted_elements = sorted(elements, key=lambda e: e.bbox[1])
        else:
            # Multi-column: group by column, sort within column by y, sort between columns by x
            page_width = max((e.bbox[2] for e in elements), default=800)
            col_width = page_width / columns

            def get_col(elem):
                center_x = (elem.bbox[0] + elem.bbox[2]) / 2
                return int(center_x / col_width)

            # Sort by column and y
            sorted_elements = sorted(elements, key=lambda e: (get_col(e), e.bbox[1]))

        # Update order numbers
        for i, elem in enumerate(sorted_elements, 1):
            elem.order = i

        return sorted_elements

    def _link_captions(self, elements: list[LayoutElement]) -> list[LayoutElement]:
        """Associate figures/tables with their captions"""
        # Find all picture/table/formula elements
        media_elements = [e for e in elements
                         if e.element_type in ("picture", "table", "formula")]
        captions = [e for e in elements if e.element_type == "caption"]

        # Simple nearest-neighbor matching: find the closest caption for each media element
        for media in media_elements:
            media_y = (media.bbox[1] + media.bbox[3]) / 2
            closest_caption = None
            min_dist = float('inf')

            for cap in captions:
                cap_y = (cap.bbox[1] + cap.bbox[3]) / 2
                dist = abs(cap_y - media_y)
                # Caption is typically within 100px above or below the image
                if dist < 150 and dist < min_dist:
                    min_dist = dist
                    closest_caption = cap

            if closest_caption:
                media.caption_ref = f"caption_{closest_caption.order}"
                closest_caption.refers_to = f"{media.element_type}_{media.order}"

        return elements

    def _detect_page_type(self, elements: list[LayoutElement]) -> str:
        """Detect page type"""
        if not elements:
            return "blank"

        total_text = ' '.join(str(e.content) for e in elements)
        total_text_lower = total_text.lower()

        # Detect cover page (V5.0: use configurable indicators)
        from ...config import settings
        page_type_config = settings.CONTEXT_CONFIG.get("page_type_detection", {})
        cover_indicators = page_type_config.get("cover_indicators", ["cover", "目录"])
        if any(kw in total_text_lower for kw in cover_indicators):
            if len(elements) < 5:
                return "cover"

        # Detect table of contents (V5.0: use configurable indicators)
        toc_indicators = page_type_config.get("toc_indicators", ["contents", "目录", "table of contents", "章节"])
        if any(kw in total_text_lower for kw in toc_indicators):
            toc_pattern_count = sum(1 for e in elements
                               if re.match(r'^[\d\.\s]+', str(e.content)))
            if toc_pattern_count >= 3:
                return "toc"

        # Detect table page (dense tables)
        table_count = sum(1 for e in elements if e.element_type == "table")
        if table_count >= 2 or (table_count >= 1 and len(elements) < 5):
            return "table_page"

        # Detect form page
        form_indicators = sum(1 for e in elements
                            if any(kw in str(e.content) for kw in
                                  ['姓名', '地址', '电话', '身份证', '号码',
                                   'name', 'address', 'tel', 'phone', 'email']))
        if form_indicators >= 3:
            return "form"

        # Detect image page
        picture_count = sum(1 for e in elements if e.element_type == "picture")
        if picture_count >= 1 and len(elements) < 4:
            return "image_page"

        # Detect appendix
        if any(kw in total_text_lower for kw in ['appendix', '附录', '附表', '参考文献']):
            return "appendix"

        # Detect blank page
        total_chars = sum(len(str(e.content)) for e in elements)
        if total_chars < 50:
            return "blank"

        return "text_body"

    def detect_columns_from_positions(self, x_positions: list[float],
                                     page_width: float) -> int:
        """Detect column count from positions (for external use)"""
        if len(x_positions) < 3:
            return 1

        x_positions = sorted(x_positions)
        gaps = [x_positions[i+1] - x_positions[i] for i in range(len(x_positions)-1)]

        if not gaps:
            return 1

        # Detect if there are obvious gap separators
        large_gaps = [g for g in gaps if g > page_width * 0.15]

        if len(large_gaps) >= 2:
            return 3
        elif len(large_gaps) == 1:
            return 2

        return 1
