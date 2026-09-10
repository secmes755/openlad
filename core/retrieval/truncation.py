"""One truncation marker, so the confidence signal can actually see it.

The answer-confidence heuristic downgrades when the context it synthesized from
was cut short. That signal used to depend on sniffing prose out of the context
string, and it was blind twice over:

* the retrieval side emitted lowercase markers (``...[truncated]``,
  ``...[content truncated]``, ``... (context truncated)``) while the check looked
  for uppercase ``[TRUNCATED`` — so retrieval truncation never registered and a
  cut-short answer could still be reported as ``high`` confidence;
* three call sites (executor standard + decomposed, engine deep-research) cut the
  context with no marker at all, so no string check could see them even in
  principle.

Both the producers and the detector now go through this module, so the marker is
written on every truncation and read in exactly one place.
"""

MARKER = "[TRUNCATED]"

# Markers written by other layers (or by earlier versions) that also mean "this
# text was cut short". Kept so existing content stays detectable.
_LEGACY_MARKERS = (
    "[CONTENT TRUNCATED",    # models/client.py prompt safety guard
    "[TRUNCATED",            # models/client.py context-size guard, and MARKER
    "[truncated]",           # retriever.py, pre-canonical
    "[content truncated]",   # retriever.py, pre-canonical
    "(context truncated)",   # synthesizer.py, pre-canonical
)


def mark_truncated(text: str) -> str:
    """Append the canonical marker to text that was cut short."""
    return f"{text}\n{MARKER}"


def is_truncated(text: str) -> bool:
    """True when the text carries any known truncation marker."""
    if not text:
        return False
    return any(marker in text for marker in _LEGACY_MARKERS)
