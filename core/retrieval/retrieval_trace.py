"""Request-scoped retrieval diagnostics — a record of what retrieval actually did.

Purpose: make a retrieval outcome explainable after the fact. It captures which
keywords were expanded, which pages the FTS channel found (with ranks), which
pages the vector channel found (with scores), what the merged top-k looked like,
and which branch the hybrid search took.

Why an explicit request-scoped object rather than attributes on the retriever:
``QueryEngine`` caches components per tenant and shares them across concurrent
requests (see the note in ``core/retrieval/executor.py``), so per-request state
must never be stored on a shared component.

Everything is bounded on purpose: ``query_log`` keeps one row per query in the
tenant database, so list fields are head-truncated and their true length is
recorded alongside as ``<field>_count`` — a truncated list is then never
mistaken for the whole result set.
"""

from __future__ import annotations

MAX_LIST_ITEMS = 12
MAX_TEXT_CHARS = 300

__all__ = ["RetrievalTrace"]


def _trim(value):
    """Cap long strings so a single field cannot bloat the stored trace."""
    if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
        return value[:MAX_TEXT_CHARS] + "..."
    return value


def _trim_list(items):
    """Head-truncate a list; the caller records the untrimmed length separately."""
    return [_trim(item) for item in list(items)[:MAX_LIST_ITEMS]]


class RetrievalTrace:
    """Accumulates retrieval diagnostics for a single request."""

    def __init__(self) -> None:
        self._sections: dict[str, dict] = {}

    def record(self, section: str, **fields) -> RetrievalTrace:
        """Merge ``fields`` into ``section``.

        Lists are head-truncated to ``MAX_LIST_ITEMS`` and their full length is
        stored as ``<key>_count``.
        """
        bucket = self._sections.setdefault(section, {})
        for key, value in fields.items():
            if isinstance(value, (list, tuple)):
                items = list(value)
                bucket[key] = _trim_list(items)
                bucket[f"{key}_count"] = len(items)
            else:
                bucket[key] = _trim(value)
        return self

    def note(self, section: str, message: str) -> RetrievalTrace:
        """Attach a short human-readable explanation to a section."""
        self._sections.setdefault(section, {})["note"] = _trim(message)
        return self

    def to_dict(self) -> dict | None:
        """Return the collected trace, or None when nothing was recorded."""
        return self._sections or None
