"""
OpenLAD Model Client - Thread-safe + Exception Propagation + Connection Reuse
Directly inherits mature implementation from predecessor, fully generalized, no industry-specific content
"""
import json
import logging
import threading
from typing import Any

import requests

from ..config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding generation failure exception"""
    pass


class ModelClient:
    """Model Client - Thread-safe"""

    def __init__(self):
        self.llm_base_url = settings.LLM_BASE_URL
        self.llm_model = settings.LLM_MODEL_NAME
        self.embedding_base_url = settings.EMBEDDING_API_BASE
        self.embedding_model = settings.EMBEDDING_MODEL_NAME
        self._session = None
        self._lock = threading.Lock()

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def generate(self, prompt: str, system_prompt: str = None,
                 max_tokens: int = 2048, temperature: float = 0.7,
                 json_mode: bool = False, json_array_mode: bool = False) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self._chat_completion(messages, max_tokens, temperature, json_mode,
                                     json_array_mode=json_array_mode)

    def generate_with_image(self, prompt: str, image_path: str,
                            system_prompt: str = None,
                            max_tokens: int = 2048, temperature: float = 0.3,
                            max_image_size: int = 1024) -> str:
        import base64 as b64
        from io import BytesIO as Bio

        try:
            lower_path = image_path.lower()
            if lower_path.endswith('.png'):
                mime_type = "image/png"
                output_format = "PNG"
            elif lower_path.endswith('.jpg') or lower_path.endswith('.jpeg'):
                mime_type = "image/jpeg"
                output_format = "JPEG"
            elif lower_path.endswith('.gif'):
                mime_type = "image/gif"
                output_format = "JPEG"
            elif lower_path.endswith('.bmp'):
                mime_type = "image/bmp"
                output_format = "JPEG"
            else:
                mime_type = "image/jpeg"
                output_format = "JPEG"

            try:
                from PIL import Image
            except ImportError:
                logger.error("PIL/Pillow not installed")
                return ""

            with Image.open(image_path) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                width, height = img.size
                max_side = max(width, height)
                if max_side > max_image_size:
                    ratio = max_image_size / max_side
                    img = img.resize(
                        (int(width * ratio), int(height * ratio)),
                        Image.LANCZOS
                    )
                buffer = Bio()
                img.save(buffer, format=output_format, quality=85)
                image_data = buffer.getvalue()

            base64_image = b64.b64encode(image_data).decode('utf-8')
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime_type};base64,{base64_image}"
                    }}
                ]
            })
            return self._chat_completion(messages, max_tokens, temperature)
        except Exception as e:
            logger.error(f"Image parsing failed {image_path}: {e}")
            return ""

    def _chat_completion(self, messages: list, max_tokens: int = 2048,
                         temperature: float = 0.7, json_mode: bool = False,
                         json_array_mode: bool = False,
                         base_url: str = None, model: str = None) -> str:
        # === Prompt length guard: prevent oversized prompts from crashing llama-server ===
        llm_max_tokens = settings.CONTEXT_CONFIG.get("llm_max_tokens", 131072)
        ratio = settings.CONTEXT_CONFIG.get("token_to_char_ratio", 0.7)
        max_prompt_chars = int(llm_max_tokens * ratio)
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                # Image messages: content is a list of text/image parts —
                # only count the text portion, skip base64 image data
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        total_chars += len(item.get("text", ""))
            else:
                total_chars += len(str(content))
        if total_chars > max_prompt_chars:
            logger.critical(
                f"[MODEL] ⚠️ INCOMING PROMPT EXCEEDS SAFE LIMIT: {total_chars} chars "
                f"(limit={max_prompt_chars}, llm_max_tokens={llm_max_tokens}, ratio={ratio}). "
                f"This would likely crash llama-server. Truncating last message..."
            )
            import traceback
            logger.critical(f"[MODEL] Oversized prompt call stack:\n{''.join(traceback.format_stack())}")
            # Truncate the last (user) message to fit
            overhead = total_chars - len(str(messages[-1].get("content", "")))
            safe_len = max_prompt_chars - overhead - 5000  # 5K safety margin for template
            if safe_len > 1000 and isinstance(messages[-1].get("content"), str):
                old_len = len(messages[-1]["content"])
                messages[-1]["content"] = messages[-1]["content"][:safe_len] + "\n\n[CONTENT TRUNCATED BY SAFETY GUARD]"
                logger.critical(f"[MODEL] Truncated last message from {old_len} to {len(messages[-1]['content'])} chars")
            else:
                logger.critical("[MODEL] Cannot safely truncate — overhead alone exceeds limit. Allowing request to proceed with CRITICAL warning.")

        payload = {
            "model": model or self.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        # FIX: Always disable thinking mode for all requests to reduce latency
        # and prevent token waste on internal reasoning text
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        if json_mode:
            if json_array_mode:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": {
                            "type": "array",
                            "items": {"type": "object"}
                        }
                    }
                }
            else:
                payload["response_format"] = {"type": "json_object"}
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        target_url = (base_url or self.llm_base_url) + "/chat/completions"
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.session.post(target_url, json=payload, timeout=300)
                # Handle "context size exceeded" errors — truncate and retry
                if response.status_code == 400:
                    error_text = response.text[:500]
                    if "context" in error_text.lower() and ("exceed" in error_text.lower() or "size" in error_text.lower()):
                        last_content = messages[-1].get("content", "") if messages else ""
                        if isinstance(last_content, str) and len(last_content) > 2000:
                            new_len = len(last_content) // 2
                            messages[-1]["content"] = last_content[:new_len] + "\n\n[TRUNCATED — context size exceeded]"
                            payload["messages"] = messages
                            logger.warning(f"[MODEL] Context exceeded. Truncating last message {len(last_content)} → {new_len} chars, retrying...")
                            continue  # retry with truncated content
                        logger.error(f"[MODEL] Context size error but last msg too short to truncate ({len(last_content)} chars)")
                        return ""
                # Detect permanent server-side errors that won't benefit from retry
                if response.status_code == 500:
                    error_text = response.text[:500].lower()
                    permanent_errors = [
                        "image input is not supported",
                        "mmproj",
                        "model not found",
                    ]
                    if any(pe in error_text for pe in permanent_errors):
                        logger.error(f"[MODEL] Permanent server error (not retrying): {error_text[:200]}")
                        return ""
                response.raise_for_status()
                data = response.json()
                msg = data["choices"][0]["message"]
                content = msg.get("content", "") or ""
                # Qwen3.5 thinking mode — if content is empty, model likely exhausted
                # tokens during thinking. Do NOT fall back to reasoning_content.
                if not content.strip() and msg.get("reasoning_content"):
                    logger.warning(f"[MODEL] content empty (reasoning_content={len(msg['reasoning_content'])} chars), "
                                   f"model may have exhausted tokens during thinking. Returning empty.")
                return content
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # exponential backoff: 1s, 2s, 4s
                    logger.warning(f"LLM call failed (attempt {attempt + 1}/{max_retries}): {e}, retrying in {wait}s...")
                    import time
                    time.sleep(wait)
                else:
                    logger.error(f"LLM call failed ({target_url}): {e}")
                    return ""

    def generate_json(self, prompt: str, system_prompt: str = None,
                      max_tokens: int = 4096, temperature: float = 0.3) -> dict[str, Any]:
        result = self._generate_json_inner(prompt, system_prompt, max_tokens, temperature)
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        if isinstance(result, dict):
            return result
        return {}

    def generate_json_array(self, prompt: str, system_prompt: str = None,
                            max_tokens: int = 4096, temperature: float = 0.3) -> list[dict]:
        result = self._generate_json_inner(prompt, system_prompt, max_tokens, temperature,
                                           json_array_mode=True)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return []

    def _generate_json_inner(self, prompt: str, system_prompt: str = None,
                              max_tokens: int = 4096, temperature: float = 0.3,
                              json_array_mode: bool = False) -> Any:
        import re

        def _try_parse(text: str) -> Any:
            if not text:
                return None
            text = text.strip()
            if text.startswith('\ufeff'):
                text = text[1:]

            def _parse_json(candidate: str) -> Any:
                candidate = candidate.strip()
                # Handle markdown code block wrapped JSON (e.g. ```json\n{...}\n```)
                if candidate.startswith('```json'):
                    candidate = candidate[7:]
                elif candidate.startswith('```'):
                    candidate = candidate[3:]
                if candidate.endswith('```'):
                    candidate = candidate[:-3]
                candidate = candidate.strip()
                try:
                    parsed = json.loads(candidate)
                    return parsed
                except Exception:
                    pass
                for brace in ['{', '[']:
                    if brace in candidate:
                        start = candidate.find(brace)
                        end_char = '}' if brace == '{' else ']'
                        depth = 0
                        end = -1
                        for i, c in enumerate(candidate[start:], start):
                            if c == brace:
                                depth += 1
                            elif c == end_char:
                                depth -= 1
                                if depth == 0:
                                    end = i + 1
                                    break
                        if end > start:
                            try:
                                parsed = json.loads(candidate[start:end])
                                return parsed
                            except Exception:
                                pass
                return None

            # Handle DeepSeek "Thinking Process:" prefix — the model outputs thinking text
            # before the JSON response. Extract and parse the JSON portion.
            if "Thinking Process:" in text[:1000]:
                # Strategy 1: find ```json code blocks (most common format)
                json_blocks = list(re.finditer(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL))
                for m in reversed(json_blocks):  # Last block is most likely the actual JSON
                    parsed = _parse_json(m.group(1))
                    if parsed is not None:
                        return parsed
                # Strategy 2: find JSON after the thinking text
                # The thinking text ends before the JSON. Look for { or [ near the end.
                for brace in ['{', '[']:
                    last_idx = text.rfind(brace)
                    if last_idx > 0:
                        try:
                            parsed = json.loads(text[last_idx:])
                            return parsed
                        except Exception:
                            pass
                # Strategy 3: fall through to standard parsing on the full text

            if "<think>" in text:
                text_without_think = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
                parsed = _parse_json(text_without_think)
                if parsed is not None:
                    return parsed
                think_match = re.search(r'<think>(.*?)</think>', text, re.DOTALL)
                if think_match:
                    parsed = _parse_json(think_match.group(1))
                    if parsed is not None:
                        return parsed
            else:
                parsed = _parse_json(text)
                if parsed is not None:
                    return parsed

            return None

        result = self.generate(prompt, system_prompt, max_tokens, temperature,
                              json_mode=True, json_array_mode=json_array_mode)
        logger.info(f"[MODEL] generate_json raw len={len(result)}, preview={result[:200]!r}")
        parsed = _try_parse(result)
        if parsed is not None:
            return parsed
        logger.error(f"[MODEL] JSON parsing failed, raw={result[:500]!r}")
        return None

    def embed(self, text: str) -> list[float]:
        """Get embedding vector"""
        max_chars = settings.EMBEDDING_CONFIG["max_embed_chars"]
        if len(text) > max_chars:
            text = text[:max_chars]
            logger.warning(f"[EMBED] Input truncated to {max_chars} chars")
        try:
            response = self.session.post(
                f"{self.embedding_base_url}/embeddings",
                json={"model": self.embedding_model, "input": text},
                timeout=60
            )
            response.raise_for_status()
            result = response.json()
            return result["data"][0]["embedding"]
        except Exception as e:
            raise EmbeddingError(f"Embedding call failed: {e}") from e

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        max_chars = settings.EMBEDDING_CONFIG["max_embed_chars"]
        truncated = [t[:max_chars] for t in texts]
        try:
            response = self.session.post(
                f"{self.embedding_base_url}/embeddings",
                json={"model": self.embedding_model, "input": truncated},
                timeout=300
            )
            response.raise_for_status()
            result = response.json()
            return [d["embedding"] for d in result["data"]]
        except Exception as e:
            raise EmbeddingError(f"Embedding batch call failed: {e}") from e

    def health_check(self) -> bool:
        try:
            response = self.session.get(f"{self.llm_base_url}/models", timeout=5)
            llm_ok = response.status_code == 200
        except Exception:
            llm_ok = False
        try:
            response = self.session.get(f"{self.embedding_base_url}/models", timeout=5)
            emb_ok = response.status_code == 200
        except Exception:
            emb_ok = False
        return llm_ok and emb_ok


_model_client = None
_model_lock = threading.Lock()


def get_model_client() -> ModelClient:
    global _model_client
    if _model_client is None:
        with _model_lock:
            if _model_client is None:
                _model_client = ModelClient()
    return _model_client
