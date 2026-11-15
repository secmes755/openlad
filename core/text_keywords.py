"""Generic keyword extraction; language-specific token shapes live in packs."""
import re


def extract_keywords(text: str, patterns: list[str] | None = None) -> list[str]:
    """Return unique, non-numeric-only tokens in their original order.

    The caller supplies pack-owned patterns. Normal callers read the generic
    pack through ``get_generic_keyword_patterns``.
    """
    if not text:
        return []
    if patterns is None:
        from .plugins import get_generic_keyword_patterns
        patterns = get_generic_keyword_patterns()
    keywords = []
    seen = set()
    for pattern in patterns:
        try:
            matches = re.finditer(pattern, text)
        except re.error:
            continue
        for match in matches:
            value = match.group(0).strip()
            if not value or value.isdigit():
                continue
            folded = value.casefold()
            if folded not in seen:
                seen.add(folded)
                keywords.append(value)
    return keywords
