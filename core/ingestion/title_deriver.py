"""Document title derivation (priority chain).

Generates an identifiable document title from the L1 document summary +
filename via structured LLM extraction, with anti-hallucination validation.

Kept dependency-free (stdlib only) so it can be unit-tested without the full
ingestion stack (Pillow/OCR/layout analyzers) and reused by any caller that
holds a model client.

Priority chain (see derive_title):
  1. explicit title (e.g. upload API param) — highest priority
  2. structured LLM extraction from L1 summary + filename
  3. None (caller falls back to filename-derived title)
"""
import logging
import re

logger = logging.getLogger(__name__)

_TITLE_MAX_LEN = 80


def subject_in_text(subject: str, text: str) -> bool:
    """Whitespace-insensitive substring check (handles line breaks in summaries)."""
    if not subject or not text:
        return False
    compact_s = re.sub(r"\s+", "", subject)
    compact_t = re.sub(r"\s+", "", text)
    return len(compact_s) >= 2 and compact_s in compact_t


def generate_identifiable_title(model_client, filename: str, summary: str,
                                classification: dict) -> str | None:
    """Generate an identifiable document title from L1 summary + filename.

    Structured extraction (NOT free-form summarization): the LLM extracts
    {subject, year, doc_type} as JSON. Every field is validated against the
    source texts (filename/summary/category) before use — a field that does
    not appear in the source is dropped (anti-hallucination gate). Returns
    None when the LLM is unavailable or no field passes validation.

    Works for any document type (annual report, employee handbook, medical
    report, EIA plan, ...) because the summary is already LLM-generated from
    full content and typically names the subject in its first sentence.
    """
    if not summary or len(summary.strip()) < 20:
        return None

    cat_text = " ".join(
        str(classification.get(k) or "") for k in
        ("category_level1", "category_level2", "category_level3")
    )
    prompt = f"""请根据文档文件名和文档内容摘要，提取该文档的标识信息。

文件名: {filename}
内容摘要: {summary}
文档分类: {cat_text}

请提取以下三个字段：
1. subject: 文档主要描述的主体（公司/组织/项目/个人名称，使用全称；无法确定填 null）
2. year: 文档内容涉及的年份（4位数字；无法确定填 null）
3. doc_type: 文档类型（如 年度报告/员工手册/体检报告/datasheet；无法确定填 null）

只输出一个 JSON 对象，不要输出任何其他内容：
{{"subject": "...", "year": null, "doc_type": "..."}}"""

    try:
        data = model_client.generate_json(prompt, max_tokens=256, temperature=0)
        if not isinstance(data, dict):
            return None
    except Exception as e:
        logger.warning(f"[TITLE] LLM title extraction failed: {e}")
        return None

    subject = str(data.get("subject") or "").strip()
    year_val = data.get("year")
    year_str = str(year_val).strip() if year_val is not None else ""
    doc_type = str(data.get("doc_type") or "").strip()

    # Validation gates (anti-hallucination): every field must appear in source
    source_text = f"{filename}\n{summary}\n{cat_text}"
    parts = []
    if subject and subject_in_text(subject, source_text):
        parts.append(subject)
    if re.fullmatch(r"\d{4}", year_str) and year_str in source_text:
        parts.append(year_str)
    if doc_type and doc_type in source_text:
        parts.append(doc_type)
    if not parts:
        return None
    new_title = " ".join(parts)
    if len(new_title) > _TITLE_MAX_LEN:
        new_title = new_title[:_TITLE_MAX_LEN]
    return new_title


def derive_title(model_client, filename: str, summary: str, classification: dict,
                 explicit_title: str | None = None) -> str | None:
    """Title derivation chain (priority):

    1. explicit title (e.g. upload API param) — highest priority
    2. structured LLM extraction from L1 summary + filename
    3. None — caller falls back to filename-derived title
    """
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()
    return generate_identifiable_title(model_client, filename, summary, classification)
