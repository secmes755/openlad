"""
Formula Recognition Module
Strategy:
1. Prefer using the already-resident multimodal LLM (no extra VRAM needed)
2. Fall back to lightweight rule matching (preserve original image)
3. Reserve pix2tex/uni_mernet interface (load on demand)

VRAM Management:
- LLM is already resident, no extra VRAM consumed
- Dedicated models only loaded on demand for batch processing
"""
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FormulaRecognizer:
    """
    Formula Recognizer
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.model = self.config.get("model", "llm_fallback")  # llm_fallback / pix2tex / uni_mernet
        self.output_format = self.config.get("output_format", "latex")
        self.keep_image = self.config.get("keep_image", True)
        self.min_confidence = self.config.get("min_confidence", 0.7)

        self._pix2tex_model = None
        self._model_client = None

    def _get_model_client(self):
        """Get multimodal LLM client"""
        if self._model_client is None:
            from ...models.client import get_model_client
            self._model_client = get_model_client()
        return self._model_client

    def recognize(self, image_path: str, formula_id: str = "") -> dict[str, Any]:
        """
        Recognize formula image

        Args:
            image_path: Cropped formula region image
            formula_id: Formula ID

        Returns:
            {
                "latex": str,
                "mathml": str,
                "image_path": str,
                "confidence": float,
                "source": str
            }
        """
        if not Path(image_path).exists():
            return {"latex": "", "image_path": image_path, "confidence": 0.0, "error": "File not found"}

        # Prefer LLM recognition (no extra VRAM needed)
        if self.model == "llm_fallback" or self.model == "pix2tex":
            result = self._recognize_with_llm(image_path)
            if result.get("latex") and result.get("confidence", 0) >= self.min_confidence:
                return result

        # Fallback: preserve original image, mark as low confidence
        return {
            "latex": "",
            "mathml": "",
            "image_path": image_path,
            "confidence": 0.0,
            "source": "unrecognized",
            "note": "Formula recognition requires a more advanced model or manual review"
        }

    def _recognize_with_llm(self, image_path: str) -> dict[str, Any]:
        """Use multimodal LLM to recognize formula"""
        try:
            client = self._get_model_client()

            prompt = """Please recognize the mathematical formula in this image and output the corresponding LaTeX code.
If there is no formula in the image, return "NO_FORMULA".
Output only the LaTeX code, no other explanations."""

            response = client.generate_with_image(
                prompt=prompt,
                image_path=image_path,
                max_tokens=512,
                temperature=0.1
            )

            if not response or response.strip() == "NO_FORMULA":
                return {"latex": "", "confidence": 0.0, "source": "llm"}

            # Extract LaTeX code
            latex = self._extract_latex(response)

            # Simple validation: check bracket matching
            confidence = self._validate_latex(latex)

            return {
                "latex": latex,
                "mathml": "",
                "image_path": image_path if self.keep_image else "",
                "confidence": confidence,
                "source": "llm"
            }

        except Exception as e:
            logger.error(f"LLM formula recognition failed: {e}")
            return {"latex": "", "confidence": 0.0, "source": "llm", "error": str(e)}

    def _extract_latex(self, text: str) -> str:
        """Extract LaTeX code from response"""
        # Check for LaTeX code block
        if "```" in text:
            import re
            match = re.search(r'```(?:latex)?\s*(.*?)\s*```', text, re.DOTALL)
            if match:
                return match.group(1).strip()

        # Check if it's pure LaTeX
        text = text.strip()
        if text.startswith('$') and text.endswith('$'):
            return text[1:-1].strip()
        if text.startswith('$$') and text.endswith('$$'):
            return text[2:-2].strip()

        return text

    def _validate_latex(self, latex: str) -> float:
        """
        Simple LaTeX syntax validation, returns confidence score
        """
        if not latex:
            return 0.0

        score = 0.5  # Base score

        # Check bracket matching
        braces = {'{': '}', '[': ']', '(': ')'}
        stack = []
        for c in latex:
            if c in braces:
                stack.append(c)
            elif c in braces.values():
                if not stack:
                    score -= 0.1
                else:
                    stack.pop()

        if not stack:
            score += 0.2
        else:
            score -= len(stack) * 0.05

        # Check for common math commands
        math_commands = ['\\frac', '\\sum', '\\int', '\\prod', '\\sqrt',
                        '\\alpha', '\\beta', '\\gamma', '\\delta', '\\pi',
                        '\\times', '\\div', '\\pm', '\\leq', '\\geq',
                        '\\infty', '\\partial', '\\nabla', '\\cdot']
        has_command = any(cmd in latex for cmd in math_commands)
        if has_command:
            score += 0.15

        # Check for equals sign (formulas typically have equals)
        if '=' in latex:
            score += 0.1

        # Check for subscripts/superscripts
        if '_' in latex or '^' in latex:
            score += 0.05

        return min(max(score, 0.0), 1.0)

    def batch_recognize(self, formula_images: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """
        Batch formula recognition

        Args:
            formula_images: [(formula_id, image_path), ...]

        Returns:
            List of recognition results
        """
        results = []
        for formula_id, image_path in formula_images:
            result = self.recognize(image_path, formula_id)
            result["formula_id"] = formula_id
            results.append(result)
        return results

    def release(self):
        """Release model resources"""
        if self._pix2tex_model is not None:
            self._pix2tex_model = None
            import gc
            gc.collect()
            logger.info("Formula recognizer model released")

    def __del__(self):
        self.release()
