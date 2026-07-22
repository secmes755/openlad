"""
PDF Watermark Detection & Removal - Generalized Approach (V2)

Detects watermarks by geometric analysis of PDF content streams:
- Rotation angle: watermarks are typically rotated (45°, -45°, etc.)
- Color: non-black (gray, semi-transparent)
- Repetition: same pattern across many pages
- Position: consistent location

Key insight: Watermarks use transformation matrices (Tm) with rotation
coefficients (b and c non-zero) to create angled text. Normal text is
horizontal (b=0, c=0) or vertical (a=0, d=0).

NO hardcoded text patterns. NO font-specific encoding assumptions.
"""
import logging
import re
import math
from collections import Counter
from typing import List, Dict, Set, Tuple, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ContentStreamAnalyzer:
    """Analyze PDF content stream to extract text blocks and their properties"""
    
    @classmethod
    def extract_text_blocks(cls, data: bytes) -> List[Dict]:
        """
        Extract text blocks from PDF content stream.
        Returns list of blocks with raw bytes, position hints, and operators.
        """
        blocks = []
        
        # Find all BT..ET blocks
        bt_positions = []
        pos = 0
        while True:
            pos = data.find(b'BT', pos)
            if pos == -1:
                break
            bt_positions.append(pos)
            pos += 2
        
        et_positions = []
        pos = 0
        while True:
            pos = data.find(b'ET', pos)
            if pos == -1:
                break
            et_positions.append(pos)
            pos += 2
        
        if len(bt_positions) != len(et_positions):
            logger.debug(f"BT/ET mismatch: {len(bt_positions)} BT, {len(et_positions)} ET")
            return []
        
        for bt, et in zip(bt_positions, et_positions):
            block_data = data[bt:et+2]
            
            # Extract text-showing operators and their operands
            text_segments = cls._extract_text_segments(block_data)
            
            # Extract transformation matrix (Tm operator = text matrix)
            # Tm takes 6 numbers: a b c d e f
            # a,d = scale, b,c = rotation/skew, e,f = translation
            tm_result = cls._extract_text_matrix(block_data)
            
            # Extract font info (Tf operator)
            font_info = cls._extract_font_info(block_data)
            
            # Extract color info (g/G/rg/RG/K operators)
            color_info = cls._extract_color_info(block_data)
            
            blocks.append({
                "raw_bytes": block_data,
                "text_segments": text_segments,
                "text_matrix": tm_result,
                "font_info": font_info,
                "color_info": color_info,
                "size": len(block_data),
            })
        
        return blocks
    
    @classmethod
    def _extract_text_segments(cls, block_data: bytes) -> List[bytes]:
        """Extract text string operands from text-showing operators"""
        segments = []
        
        i = 0
        while i < len(block_data):
            # Skip whitespace
            while i < len(block_data) and block_data[i:i+1] in b' \t\n\r\x00':
                i += 1
            if i >= len(block_data):
                break
            
            # Check for parenthesized string: ( ... )
            if block_data[i:i+1] == b'(':
                j = i + 1
                depth = 1
                while j < len(block_data) and depth > 0:
                    if block_data[j:j+1] == b'\\':
                        j += 2  # Skip escaped char
                    elif block_data[j:j+1] == b'(':
                        depth += 1
                        j += 1
                    elif block_data[j:j+1] == b')':
                        depth -= 1
                        j += 1
                    else:
                        j += 1
                if depth == 0:
                    string_data = block_data[i+1:j-1]
                    # Check if followed by text operator
                    after = block_data[j:j+10].lstrip(b' \t\n\r\x00')
                    if any(after.startswith(op) for op in [b'Tj', b'TJ', b"'", b'"']):
                        segments.append(string_data)
                i = j
            # Check for hex string: <hex>
            elif block_data[i:i+1] == b'<':
                j = i + 1
                while j < len(block_data) and block_data[j:j+1] != b'>':
                    j += 1
                if j < len(block_data):
                    hex_data = block_data[i+1:j]
                    after = block_data[j+1:j+11].lstrip(b' \t\n\r\x00')
                    if any(after.startswith(op) for op in [b'Tj', b'TJ']):
                        segments.append(hex_data)
                i = j + 1
            else:
                i += 1
        
        return segments
    
    @classmethod
    def _extract_text_matrix(cls, block_data: bytes) -> Optional[Dict]:
        """
        Extract text transformation matrix from Tm operator.
        
        Tm takes 6 numbers: a b c d e f
        The matrix is:
            [ a  b  0 ]
            [ c  d  0 ]
            [ e  f  1 ]
        
        For text rendering:
        - a, d = scale factors (font size)
        - b, c = rotation coefficients
        - e, f = translation (position)
        
        Rotation angle = atan2(c, a) or atan2(-b, d)
        """
        # Pattern: num num num num num num Tm
        tm_pattern = re.compile(
            rb'([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+Tm'
        )
        match = tm_pattern.search(block_data)
        if match:
            try:
                a = float(match.group(1))
                b = float(match.group(2))
                c = float(match.group(3))
                d = float(match.group(4))
                e = float(match.group(5))  # x position
                f = float(match.group(6))  # y position
                
                # Calculate rotation angle in degrees
                # For a rotation matrix: [cosθ  sinθ; -sinθ  cosθ]
                # So a = cosθ, b = sinθ, c = -sinθ, d = cosθ
                # Rotation angle = atan2(b, a) or atan2(-c, d)
                angle_rad = math.atan2(b, a)
                angle_deg = math.degrees(angle_rad)
                
                # Normalize to [-180, 180]
                while angle_deg > 180:
                    angle_deg -= 360
                while angle_deg < -180:
                    angle_deg += 360
                
                # Calculate scale (approximate font size)
                scale_x = math.sqrt(a*a + b*b)
                scale_y = math.sqrt(c*c + d*d)
                
                return {
                    "a": a, "b": b, "c": c, "d": d, "e": e, "f": f,
                    "angle_deg": angle_deg,
                    "scale_x": scale_x,
                    "scale_y": scale_y,
                    "position": (e, f),
                }
            except Exception:
                pass
        return None
    
    @classmethod
    def _extract_font_info(cls, block_data: bytes) -> Optional[Dict]:
        """Extract font name and size from Tf operator"""
        tf_pattern = re.compile(rb'/(\w+)\s+([\d.+-]+)\s+Tf')
        match = tf_pattern.search(block_data)
        if match:
            return {
                "font_name": match.group(1).decode('ascii', errors='ignore'),
                "font_size": float(match.group(2))
            }
        return None
    
    @classmethod
    def _extract_color_info(cls, block_data: bytes) -> Optional[Dict]:
        """
        Extract color information from color operators.
        
        g/G = gray (0-1), rg/RG = RGB (0-1 each), k/K = CMYK
        Watermarks often use non-black colors (gray < 1.0)
        """
        # Gray: num g or num G
        gray_pattern = re.compile(rb'([\d.]+)\s+[gG]')
        match = gray_pattern.search(block_data)
        if match:
            try:
                gray = float(match.group(1))
                return {"type": "gray", "value": gray, "is_black": gray < 0.3}
            except:
                pass
        
        # RGB: num num num rg or num num num RG
        rgb_pattern = re.compile(rb'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+[rR][gG]')
        match = rgb_pattern.search(block_data)
        if match:
            try:
                r, g, b = float(match.group(1)), float(match.group(2)), float(match.group(3))
                is_black = r < 0.3 and g < 0.3 and b < 0.3
                return {"type": "rgb", "r": r, "g": g, "b": b, "is_black": is_black}
            except:
                pass
        
        return None


class WatermarkDetector:
    """
    Detect watermarks by geometric analysis across pages.
    
    Key insight: Watermarks are characterized by:
    1. Rotation angle: typically 45°, -45°, or other non-0° angles
    2. Non-black color: gray or semi-transparent
    3. Repetition: same pattern on many pages
    4. Position consistency: fixed location
    """
    
    def __init__(self,
                 min_repetition_ratio: float = 0.7,
                 position_tolerance: float = 5.0,
                 min_block_size: int = 10,
                 max_block_size: int = 5000,
                 angle_threshold: float = 15.0,  # degrees: > 15° from horizontal/vertical
                 ):
        self.min_repetition_ratio = min_repetition_ratio
        self.position_tolerance = position_tolerance
        self.min_block_size = min_block_size
        self.max_block_size = max_block_size
        self.angle_threshold = angle_threshold
    
    def analyze_document(self, page_blocks: List[List[Dict]]) -> Dict:
        """
        Analyze all pages to find watermark candidates.
        
        Returns:
            Detection results with watermark candidates
        """
        total_pages = len(page_blocks)
        if total_pages == 0:
            return {"has_watermark": False, "candidates": []}
        
        # Step 1: Score each block by watermark likelihood
        block_scores = []  # (page_idx, block_idx, score, features)
        
        for page_idx, blocks in enumerate(page_blocks):
            for block_idx, block in enumerate(blocks):
                size = block["size"]
                if size < self.min_block_size or size > self.max_block_size:
                    continue
                
                features = self._analyze_block_features(block)
                score = self._calculate_watermark_score(features)
                
                block_scores.append({
                    "page_idx": page_idx,
                    "block_idx": block_idx,
                    "score": score,
                    "features": features,
                    "block": block,
                })
        
        # Step 2: Group by signature and check repetition
        signature_counts = Counter()
        signature_details = {}
        
        for bs in block_scores:
            if bs["score"] < 0.3:  # Skip low-score blocks
                continue
            
            sig = self._create_signature(bs["block"], bs["features"])
            if sig:
                signature_counts[sig] += 1
                if sig not in signature_details:
                    signature_details[sig] = {
                        "pages": [],
                        "positions": [],
                        "angles": [],
                        "scores": [],
                        "blocks": [],
                    }
                signature_details[sig]["pages"].append(bs["page_idx"])
                
                tm = bs["block"].get("text_matrix")
                if tm:
                    signature_details[sig]["positions"].append(tm["position"])
                    signature_details[sig]["angles"].append(tm["angle_deg"])
                
                signature_details[sig]["scores"].append(bs["score"])
                signature_details[sig]["blocks"].append(bs["block"])
        
        # Step 3: Identify watermark candidates
        candidates = []
        min_pages = max(2, int(total_pages * self.min_repetition_ratio))
        
        for sig, count in signature_counts.items():
            if count >= min_pages:
                details = signature_details[sig]
                
                # Check position consistency
                position_consistency = self._check_position_consistency(details["positions"])
                
                # Check angle consistency (watermarks have consistent angle)
                angle_consistency = self._check_angle_consistency(details["angles"])
                
                # Average score
                avg_score = sum(details["scores"]) / len(details["scores"])
                
                # Watermark confidence
                is_watermark = (
                    avg_score > 0.5 and
                    position_consistency > 0.5 and
                    angle_consistency > 0.7
                ) or count >= total_pages * 0.9
                
                candidates.append({
                    "signature": sig,
                    "page_count": count,
                    "total_pages": total_pages,
                    "repetition_ratio": count / total_pages,
                    "position_consistency": position_consistency,
                    "angle_consistency": angle_consistency,
                    "avg_score": avg_score,
                    "is_watermark": is_watermark,
                    "avg_size": sum(b["size"] for b in details["blocks"]) / len(details["blocks"]),
                    "sample_pages": list(set(details["pages"]))[:5],
                })
        
        # Sort by watermark likelihood
        candidates.sort(
            key=lambda c: (c["is_watermark"], c["avg_score"], c["repetition_ratio"]),
            reverse=True
        )
        
        has_watermark = any(c["is_watermark"] for c in candidates)
        
        return {
            "has_watermark": has_watermark,
            "candidates": candidates,
            "total_pages": total_pages,
        }
    
    def _analyze_block_features(self, block: Dict) -> Dict:
        """Analyze a block for watermark characteristics"""
        features = {
            "has_rotation": False,
            "rotation_angle": 0.0,
            "is_non_black": False,
            "color_gray": 1.0,
            "has_repeated_text": False,
            "text_repetition_count": 1,
        }
        
        # Check rotation angle
        tm = block.get("text_matrix")
        if tm:
            angle = tm["angle_deg"]
            features["rotation_angle"] = angle
            # Watermark: not horizontal (0°) and not vertical (90°/-90°)
            is_horizontal = abs(angle) < self.angle_threshold or abs(angle - 180) < self.angle_threshold
            is_vertical = abs(angle - 90) < self.angle_threshold or abs(angle + 90) < self.angle_threshold
            features["has_rotation"] = not is_horizontal and not is_vertical
        
        # Check color
        color = block.get("color_info")
        if color:
            if color["type"] == "gray":
                features["color_gray"] = color["value"]
                features["is_non_black"] = color["value"] > 0.3  # Not pure black
            elif color["type"] == "rgb":
                avg = (color["r"] + color["g"] + color["b"]) / 3
                features["color_gray"] = avg
                features["is_non_black"] = avg > 0.3
        
        # Check text repetition within block
        segments = block.get("text_segments", [])
        if len(segments) > 1:
            # Count unique vs total
            unique = set(repr(s) for s in segments)
            features["has_repeated_text"] = len(unique) < len(segments)
            features["text_repetition_count"] = len(segments) / max(len(unique), 1)
        
        return features
    
    def _calculate_watermark_score(self, features: Dict) -> float:
        """Calculate watermark likelihood score (0-1)"""
        score = 0.0
        
        # Rotation is the strongest signal
        if features["has_rotation"]:
            score += 0.4
            # Bonus for diagonal-ish angles (30°-60° or 120°-150°)
            angle = abs(features["rotation_angle"])
            # Normalize to [0, 90] for checking diagonal range
            angle_mod = angle % 90
            if 15 <= angle_mod <= 75:
                score += 0.2
            # Extra bonus for near-45° (most common watermark angle)
            if 30 <= angle_mod <= 60:
                score += 0.1
        
        # Non-black color (watermarks are often gray/semi-transparent)
        if features["is_non_black"]:
            score += 0.15
        
        # Repeated text within block (watermarks repeat same text many times)
        if features["has_repeated_text"] and features["text_repetition_count"] > 2:
            score += 0.15
        
        # Position-based clues: watermarks often appear in page center or corners
        # This is captured by position consistency in the detector, not here
        
        return min(score, 1.0)
    
    def _create_signature(self, block: Dict, features: Dict) -> Optional[str]:
        """Create a signature for grouping similar blocks"""
        tm = block.get("text_matrix")
        if not tm:
            return None
        
        # Round angle to nearest 15 degrees for grouping (wider tolerance)
        angle = tm["angle_deg"]
        rounded_angle = round(angle / 15) * 15
        
        # Round position to nearest 50 units for grouping
        e, f = tm["position"]
        rounded_e = round(e / 50) * 50
        rounded_f = round(f / 50) * 50
        
        # Include color info
        color = block.get("color_info")
        color_key = "unknown"
        if color:
            if color["type"] == "gray":
                color_key = f"gray{round(color['value'] * 10)}"
            elif color["type"] == "rgb":
                avg = round((color["r"] + color["g"] + color["b"]) / 3 * 10)
                color_key = f"rgb{avg}"
        
        # Signature: angle + coarse position + color
        return f"angle:{rounded_angle}:pos:{rounded_e}:{rounded_f}:color:{color_key}"
    
    def _check_position_consistency(self, positions: List[Tuple[float, float]]) -> float:
        """Check if positions are consistent"""
        if len(positions) < 2:
            return 0.0
        
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        
        x_range = max(xs) - min(xs)
        y_range = max(ys) - min(ys)
        
        if x_range <= self.position_tolerance and y_range <= self.position_tolerance:
            return 1.0
        
        max_range = max(x_range, y_range)
        if max_range == 0:
            return 1.0
        
        return 1.0 - min(max_range / 100.0, 1.0)
    
    def _check_angle_consistency(self, angles: List[float]) -> float:
        """Check if rotation angles are consistent"""
        if len(angles) < 2:
            return 0.0
        
        # Normalize angles to [0, 360) for comparison
        normalized = [(a % 360 + 360) % 360 for a in angles]
        
        # Check if all angles are similar (within 10 degrees)
        max_diff = max(normalized) - min(normalized)
        
        if max_diff <= 10:
            return 1.0
        
        # Also check for 180° symmetry (same angle, opposite direction)
        # This handles cases where some pages have 45° and others have -135° (same visual)
        
        return max(0.0, 1.0 - max_diff / 90.0)


class PDFWatermarkRemover:
    """
    Generalized PDF watermark remover.
    
    Uses geometric detection (rotation angle + color + repetition) to identify
    and remove watermark content streams from PDF pages.
    """
    
    def __init__(self, detector: Optional[WatermarkDetector] = None):
        self.detector = detector or WatermarkDetector()
    
    @classmethod
    def process_pdf(cls, input_path: str, output_path: str) -> bool:
        """
        Process entire PDF and remove detected watermarks.
        
        Returns:
            True if watermarks were detected and removed, False otherwise
        """
        try:
            from pypdf import PdfReader, PdfWriter
            
            reader = PdfReader(input_path)
            writer = PdfWriter()
            
            total_pages = len(reader.pages)
            if total_pages == 0:
                return False
            
            # Step 1: Analyze all pages
            page_blocks = []
            for page in reader.pages:
                blocks = cls._extract_page_blocks(page)
                page_blocks.append(blocks)
            
            # Step 2: Detect watermarks
            detector = WatermarkDetector()
            detection = detector.analyze_document(page_blocks)
            
            if not detection["has_watermark"]:
                logger.info(f"No watermarks detected in {total_pages} pages")
                # No watermarks found - copy original file to output path
                import shutil
                shutil.copy(input_path, output_path)
                return False
            
            # Step 3: Get watermark signatures to remove
            watermark_signatures = set()
            for candidate in detection["candidates"]:
                if candidate["is_watermark"]:
                    watermark_signatures.add(candidate["signature"])
                    logger.info(
                        f"Watermark detected: {candidate['repetition_ratio']:.1%} of pages, "
                        f"angle consistency={candidate['angle_consistency']:.2f}, "
                        f"score={candidate['avg_score']:.2f}, "
                        f"avg size={candidate['avg_size']:.0f} bytes"
                    )
            
            # Step 4: Remove watermark blocks from each page
            removed_count = 0
            for page_idx, page in enumerate(reader.pages):
                if cls._remove_watermark_blocks_from_page(page, watermark_signatures, detector):
                    removed_count += 1
                writer.add_page(page)
            
            writer.write(output_path)
            
            logger.info(f"Removed watermarks from {removed_count}/{total_pages} pages")
            return removed_count > 0
            
        except Exception as e:
            logger.warning(f"PDF watermark removal failed: {e}")
            return False
    
    @classmethod
    def _extract_page_blocks(cls, page) -> List[Dict]:
        """Extract text blocks from a PDF page"""
        try:
            contents = page.get('/Contents')
            if not contents:
                return []
            
            blocks = []
            
            # Handle array of content streams
            if hasattr(contents, '__iter__') and not isinstance(contents, (str, bytes)):
                for item in contents:
                    obj = item
                    if hasattr(item, 'get_object'):
                        obj = item.get_object()  # type: ignore
                    if hasattr(obj, 'get_data'):
                        data = obj.get_data()  # type: ignore
                        page_blocks = ContentStreamAnalyzer.extract_text_blocks(data)
                        blocks.extend(page_blocks)
            else:
                # Single content stream
                obj = contents
                if hasattr(contents, 'get_object'):
                    obj = contents.get_object()  # type: ignore
                if hasattr(obj, 'get_data'):
                    data = obj.get_data()  # type: ignore
                    page_blocks = ContentStreamAnalyzer.extract_text_blocks(data)
                    blocks.extend(page_blocks)
            
            return blocks
        except Exception as e:
            logger.debug(f"Failed to extract page blocks: {e}")
            return []
    
    @classmethod
    def _remove_watermark_blocks_from_page(cls, page, watermark_signatures: Set[str],
                                           detector: WatermarkDetector) -> bool:
        """Remove watermark blocks from a single page"""
        try:
            from pypdf.generic import NameObject, ArrayObject
            
            contents = page.get('/Contents')
            if not contents:
                return False
            
            removed = False
            
            # Handle array of content streams (most common for watermarked PDFs)
            if hasattr(contents, '__iter__') and not isinstance(contents, (str, bytes)):
                new_contents = []
                
                for item in contents:
                    obj = item
                    if hasattr(item, 'get_object'):
                        obj = item.get_object()  # type: ignore
                    if hasattr(obj, 'get_data'):
                        data = obj.get_data()  # type: ignore
                        blocks = ContentStreamAnalyzer.extract_text_blocks(data)
                        
                        # Check if any block in this stream matches watermark signature
                        is_watermark_stream = False
                        for block in blocks:
                            features = detector._analyze_block_features(block)
                            score = detector._calculate_watermark_score(features)
                            sig = detector._create_signature(block, features)
                            if sig in watermark_signatures:
                                is_watermark_stream = True
                                logger.debug(f"Removing watermark stream (signature: {sig}, score: {score:.2f})")
                                break
                        
                        if not is_watermark_stream:
                            new_contents.append(item)
                        else:
                            removed = True
                    else:
                        new_contents.append(item)
                
                if removed and len(new_contents) > 0:
                    page[NameObject('/Contents')] = ArrayObject(new_contents)
                    return True
                elif removed and len(new_contents) == 0:
                    logger.warning("All content streams were watermarks, keeping original")
                    return False
            else:
                # Single content stream - need to surgically remove BT..ET blocks
                obj = contents
                if hasattr(contents, 'get_object'):
                    obj = contents.get_object()  # type: ignore
                if hasattr(obj, 'get_data'):
                    data = obj.get_data()  # type: ignore
                    blocks = ContentStreamAnalyzer.extract_text_blocks(data)
                    
                    # Find watermark block indices
                    watermark_indices = set()
                    for i, block in enumerate(blocks):
                        features = detector._analyze_block_features(block)
                        sig = detector._create_signature(block, features)
                        if sig in watermark_signatures:
                            watermark_indices.add(i)
                    
                    if watermark_indices:
                        new_data = cls._remove_blocks_by_index(data, blocks, watermark_indices)
                        if new_data != data:
                            from pypdf.generic import ContentStream
                            new_content = ContentStream(new_data, page)
                            page[NameObject('/Contents')] = new_content
                            return True
            
            return removed
            
        except Exception as e:
            logger.warning(f"Watermark removal from page failed: {e}")
            return False
    
    @classmethod
    def _remove_blocks_by_index(cls, data: bytes, blocks: List[Dict],
                                 watermark_indices: Set[int]) -> bytes:
        """Remove specific BT..ET blocks from content stream by index"""
        # Find all BT..ET positions
        bt_positions = []
        pos = 0
        while True:
            pos = data.find(b'BT', pos)
            if pos == -1:
                break
            bt_positions.append(pos)
            pos += 2
        
        et_positions = []
        pos = 0
        while True:
            pos = data.find(b'ET', pos)
            if pos == -1:
                break
            et_positions.append(pos)
            pos += 2
        
        if len(bt_positions) != len(et_positions):
            return data
        
        # Build new content without watermark blocks
        new_data = b''
        last_end = 0
        
        for i, (bt, et) in enumerate(zip(bt_positions, et_positions)):
            if i in watermark_indices:
                new_data += data[last_end:bt]
                last_end = et + 2
        
        new_data += data[last_end:]
        return new_data


# Convenience function for direct use
def remove_pdf_watermark(input_path: str, output_path: str) -> bool:
    """Remove watermarks from PDF file using geometric detection"""
    return PDFWatermarkRemover.process_pdf(input_path, output_path)
