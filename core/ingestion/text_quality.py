"""Text-layer quality: how much of a page is font glyph codes rather than words.

Some PDFs embed fonts with no ToUnicode map, so the text layer holds glyph
indices instead of characters and a reader renders them as ``(cid:NNN)``. The
result is *ordinary ASCII* — no replacement characters, no control characters —
so a check that looks at character shapes sees nothing wrong with it.
``TextQualityChecker._detect_garbled`` counts replacement and control characters,
and therefore scores such a page as clean; that is why these documents reached the
fact index while passing every quality gate, and why the measure here is not "are
these characters valid" but "how much of the page is glyph codes instead of
prose".

Consumers decide their own thresholds, because the right one depends on what they
do with the text: building an assertion out of a garbled line is unrecoverable,
while a page that is 5% glyph codes may still hold perfectly good prose.
"""
import re

# pypdf / pdfminer render an unmapped CID as `(cid:NNN)`.
UNMAPPED_GLYPH_RE = re.compile(r"\(cid:\d+\)")


def unmapped_glyph_ratio(text: str) -> float:
    """Share of ``text`` taken up by unmapped glyph codes, in 0.0-1.0.

    Counts the glyph tokens themselves, so a dense run scores close to 1.0 and
    prose with the occasional glyph scores near 0.0.
    """
    if not text:
        return 0.0
    covered = sum(match.end() - match.start() for match in UNMAPPED_GLYPH_RE.finditer(text))
    return covered / len(text)
