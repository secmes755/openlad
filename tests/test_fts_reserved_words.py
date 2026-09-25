"""FTS reserved words in user queries must be literal text, not operators.

TenantMetadataDB.search_fts_chunks built MATCH expressions by joining raw
tokens with AND/OR. FTS5 parses uppercase AND/OR/NOT as operators, so a
datasheet-style query like "RAM AND ROM 区别" produced the expression
``RAM AND AND AND ROM`` — a syntax error in both the AND and the OR
channel, logged as a warning and swallowed, leaving zero FTS results and
a silent "not found" answer for a document that contains the exact terms.

The fix quotes every token as an FTS5 phrase (``"RAM" AND "AND" AND
"ROM"``), the same pattern agentic_retriever already uses, so reserved
words match as literal text. Quoting does not change which documents
match for ordinary tokens, so FTS precision is preserved.
"""
from core.db.tenant_db import TenantMetadataDB


def _seeded_db(tmp_path):
    db = TenantMetadataDB(tmp_path / "meta.db")
    db.save_chunk("d1", 1, 1, 0, "", "",
                  "The RAM AND ROM differences are explained in this section.")
    db.save_chunk("d1", 1, 2, 1, "", "",
                  "An unrelated paragraph about humidity sensors.")
    return db


def test_and_token_matches_as_literal(tmp_path):
    db = _seeded_db(tmp_path)
    hits = db.search_fts_chunks("RAM AND ROM", limit=10)
    assert hits, "FTS returned nothing for a query containing the reserved word AND"
    assert any("RAM AND ROM" in h["chunk_text"] for h in hits)


def test_not_token_matches_as_literal(tmp_path):
    db = TenantMetadataDB(tmp_path / "meta.db")
    db.save_chunk("d1", 1, 1, 0, "", "",
                  "This is NOT voltage but current limiting behavior.")
    db.save_chunk("d1", 1, 2, 1, "", "",
                  "An unrelated paragraph about humidity sensors.")
    hits = db.search_fts_chunks("NOT voltage", limit=10)
    assert hits, "FTS returned nothing for a query containing the reserved word NOT"
    assert any("NOT voltage" in h["chunk_text"] for h in hits)


def test_ordinary_queries_still_match(tmp_path):
    """Quoting must not weaken normal matching: an ordinary multi-token
    query still finds the chunk containing those tokens (the designed OR
    supplement may add recall, but the AND-relevant chunk must surface)."""
    db = _seeded_db(tmp_path)
    hits = db.search_fts_chunks("RAM ROM", limit=10)
    assert hits, "ordinary quoted tokens no longer match"
    assert any("RAM AND ROM" in h["chunk_text"] for h in hits)


def test_or_fallback_still_works_with_reserved_words(tmp_path):
    """When AND finds nothing, the OR supplement must also survive reserved
    words instead of erroring out."""
    db = TenantMetadataDB(tmp_path / "meta.db")
    db.save_chunk("d1", 1, 1, 0, "", "", "standalone AND gate reference")
    hits = db.search_fts_chunks("AND nonexistentterm", limit=10)
    assert hits, "OR fallback produced nothing when AND was exhausted"
    assert any("AND gate" in h["chunk_text"] for h in hits)
