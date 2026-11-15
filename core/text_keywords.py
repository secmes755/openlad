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


def extract_search_keywords(text: str, max_terms: int = 64) -> list[str]:
    """Broaden FTS terms with bounded pack-declared substring n-grams.

    Base keywords remain first. Only search lookup uses this broader set;
    ranking, evidence selection and answer logic keep the narrower tokens.
    """
    keywords = extract_keywords(text)
    if not text or max_terms <= len(keywords):
        return keywords[:max_terms] if max_terms > 0 else []
    from .plugins import get_generic_search_ngram_rules
    seen = {keyword.casefold() for keyword in keywords}
    for rule in get_generic_search_ngram_rules():
        pattern = rule.get("pattern")
        sizes = rule.get("sizes", [])
        if not isinstance(pattern, str) or not isinstance(sizes, list):
            continue
        widths = sorted({int(size) for size in sizes
                         if isinstance(size, int) and 2 <= size <= 8})
        try:
            matches = list(re.finditer(pattern, text))
        except re.error:
            continue
        for match in matches:
            token = match.group(0)
            for width in widths:
                for start in range(0, max(0, len(token) - width + 1)):
                    value = token[start:start + width]
                    folded = value.casefold()
                    if value and not value.isdigit() and folded not in seen:
                        seen.add(folded)
                        keywords.append(value)
                        if len(keywords) >= max_terms:
                            return keywords
    return keywords
