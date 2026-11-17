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
    keywords, derived = split_search_keywords(text, max_terms)
    return keywords + derived


def needs_search_ngrams(keywords: list[str]) -> bool:
    """True when the base keywords alone cannot be searched by an FTS index.

    Chinese is written without spaces, so a question such as
    "在美的集团的公司中文名称是什么" yields one 19-character "word". No index
    matches that; the substring n-grams are what make it findable, which is why
    the mechanism exists. When the extractor already produced short, searchable
    terms, the same substring explosion only competes for the term budget, so it
    is not generated.
    """
    if not keywords:
        return False
    for term in keywords:
        if len(term) <= 4:
            return False
        if not all('\u4e00' <= ch <= '\u9fff' for ch in term):
            return False
    return True


def split_search_keywords(text: str, max_terms: int = 64) -> tuple[list[str], list[str]]:
    """Return ``(base keywords, derived n-grams)`` separately.

    Callers that prioritise terms need to distinguish what the user actually
    wrote from the n-grams generated to widen recall. Deriving that boundary
    from list position would be a guess; the two groups are different in kind,
    so they are returned as different values.

    n-grams are produced only when the base keywords are unsearchable on their
    own; see :func:`needs_search_ngrams`.
    """
    keywords = extract_keywords(text)
    derived: list[str] = []
    if not text or max_terms <= 0:
        return [], []
    if not needs_search_ngrams(keywords):
        return keywords, []
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
                        derived.append(value)
                        if len(keywords) + len(derived) >= max_terms:
                            return keywords, derived
    return keywords, derived
