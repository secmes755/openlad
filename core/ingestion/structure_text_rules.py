"""Text-rule structure-index hygiene helpers (stdlib only).

Extracted from builder._build_structure_from_text so the filtering rules
are unit-testable without importing the heavy ingestion pipeline (PIL,
layout analyzers, model clients). builder.py delegates to these pure
functions; nothing here touches the database or the network.
"""
import re

# Fix 3c: title ends with a bare number, a unit, or a divider token
_TRAILING_TOKEN_RE = re.compile(
    r"(?:%|mg|L|ml|kg|元|万|亿|N/A|NA|—|–|--|…|\d+(?:[,.]\d+)*)$"
)
# Fix 3b: two consecutive pure-numeric tokens (year/table-number runs)
_TWO_DIGIT_TOKENS_RE = re.compile(r"\d+(?:[,.]\d+)*\s+\d+(?:[,.]\d+)*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_DIGIT_RE = re.compile(r"\d")
# Fix 3e: sentence boundary inside the "title" ("...was even larger. The
# data...") — body-text fragments, never real headings
_SENTENCE_BOUNDARY_RE = re.compile(r"\.\s+[A-Z]")


def numeric_title_is_heading(num_prefix: str, title: str) -> bool:
    """Decide whether a "123 title" line is a real heading (R1 junk filter).

    The input is the raw "digits + separator + rest" match from the
    appendix-oriented rule. Any of these hits rejects the line:

      a. fewer than 1 alphabetic word -> junk. The threshold is < 1, not
         < 2: "1.1 Overview" / "2.2 2023 Results" are valid headings with
         a single alphabetic word, and rejecting them would make real
         chapters unreachable (the exact failure this filter prevents).
      b. two consecutive pure-numeric tokens (year runs, table number
         columns) — e.g. "2022 2023 2022", "147 9,946,276 N/A".
      c. title ends with a bare number, a unit token (% mg L ml kg 元 万 亿
         N/A NA — – -- …), or numeric fragment — e.g. "26 March 2024 ———",
         cell-tail values.
      d. digit characters are more than 40% of the title.
      e. title contains a sentence boundary (". The" / ". Data") — a body
         text fragment, not a heading.

    CJK titles are always accepted — the Chinese appendix convention
    "40、营业收入和营业成本" must keep working unchanged.
    """
    if not title:
        return False
    if _CJK_RE.search(title):
        return True
    words = _WORD_RE.findall(title)
    if len(words) < 1:  # a
        return False
    if _TWO_DIGIT_TOKENS_RE.search(title):  # b
        return False
    if _TRAILING_TOKEN_RE.search(title):  # c
        return False
    if title and len(_DIGIT_RE.findall(title)) / len(title) > 0.4:  # d
        return False
    if _SENTENCE_BOUNDARY_RE.search(title):  # e
        return False
    return True


def chapter_heading_title_is_valid(title: str) -> bool:
    """R2 fallback: reject empty titles or titles that themselves begin with
    Chapter/Section/Part (page-header stitch lines like "Chapter 5 Chapter 5"
    match the outer pattern with group(3) == "Chapter 5"). Also used to
    reject the same stitch residue when harvesting chapter titles from TOC
    pages."""
    if not title:
        return False
    if re.match(r"^(Chapter|Section|Part)\b", title, re.IGNORECASE):
        return False
    return True


def compose_parent_chapter(chapter_num: int, parent_title: str) -> str:
    """Build the inherited parent-chapter label for a numbered sub-section.

    Never double-prefix: if the recorded title already carries the unit
    prefix (page-header stitch residue, e.g. "Chapter 5"), return it
    verbatim — otherwise prefix once ("Chapter 5 Financial Statements")."""
    if re.match(r"^(Chapter|Section|Part)\b", parent_title, re.IGNORECASE):
        return parent_title
    return f"Chapter {chapter_num} {parent_title}"


def build_repeated_lines(page_results: dict[int, dict]) -> set[str]:
    """Fix 2a: lines that appear verbatim on >= 3 different pages.

    Running headers, cross-page table headers and footers repeat verbatim;
    real headings almost never do, so the false-positive rate is
    negligible. Lines shorter than 5 chars are skipped — page numbers and
    other short fragments are handled by the numeric-title filter instead.
    """
    from collections import defaultdict

    line_pages = defaultdict(set)
    for pn, r in page_results.items():
        text = r.get("page_text", "")
        if not text:
            continue
        seen = set()
        for raw in text.split("\n"):
            line = raw.strip()
            if len(line) > 4 and line not in seen:
                seen.add(line)
                line_pages[line].add(pn)
    return {line for line, pages in line_pages.items() if len(pages) >= 3}


def ensure_page_coverage(merged_sections: list[tuple], max_page: int) -> list[tuple]:
    """Fix 1: coverage invariant — every page in [1, max_page] must belong
    to at least one [start_page, end_page] interval.

    Uncovered (orphan) pages are merged into the nearest preceding section
    (extend its end_page); if a following section starts closer, the orphan
    is prepended to it instead. If no section exists at all, a coarse
    fallback section '_body' / 'Document Body' covering every page is
    created, so retrieval can never be blocked by an unreachable page range.

    merged_sections entries:
        (short_path, start_page, end_page, level, title, full_path)
    """
    if max_page < 1:
        return merged_sections
    if not merged_sections:
        return [("_body", 1, max_page, 1, "Document Body", "_body")]

    sections = [list(s) for s in merged_sections]
    covered = set()
    for s in sections:
        for p in range(max(s[1], 1), s[2] + 1):
            covered.add(p)

    gaps = [p for p in range(1, max_page + 1) if p not in covered]
    for p in gaps:
        prev = max((s for s in sections if s[2] < p), key=lambda s: s[2], default=None)
        nxt = min((s for s in sections if s[1] > p), key=lambda s: s[1], default=None)
        if prev and nxt:
            if p - prev[2] <= nxt[1] - p:
                prev[2] = p
            else:
                nxt[1] = p
        elif prev:
            prev[2] = p
        elif nxt:
            nxt[1] = p
        # else unreachable: a non-empty section list always has a preceding
        # (end < p) or following (start > p) interval for any gap.

    # Re-sort by start_page so the invariant holds for downstream iteration.
    sections.sort(key=lambda s: s[1])
    return [tuple(s) for s in sections]


def coverage_invariant_holds(merged_sections: list[tuple], max_page: int) -> bool:
    """Assertion helper used by tests (the 'permanent vaccine'): every page
    in [1, max_page] is covered by at least one section interval."""
    if max_page < 1:
        return True
    covered = set()
    for s in merged_sections:
        for p in range(max(s[1], 1), s[2] + 1):
            covered.add(p)
    return all(p in covered for p in range(1, max_page + 1))
