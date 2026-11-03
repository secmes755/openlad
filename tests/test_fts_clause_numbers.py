"""Clause numbers must survive query sanitising as a single reference.

The sanitizer replaces every non-word character with a space, so a query for
clause "3.4.5" reached the channel as the three single-character tokens 3, 4
and 5. Both the trigram path (minimum length 3) and the bigram path (exactly
length 2) drop those tokens, so the channel returned nothing at all: a
document containing the clause could never be found by its number.

Dotted numerals are now held aside before sanitising and restored afterwards,
so they reach FTS intact and match as a phrase.
"""
from core.db.tenant_db import TenantMetadataDB


def _seeded_db(tmp_path):
    db = TenantMetadataDB(tmp_path / "meta.db")
    db.save_chunk("d1", 1, 189, 0, "Annex H", "Annex H",
                  "Clause 3.4.5 requires the appliance to withstand 6.5 kV "
                  "impulse. See table 11.2.9 for the limits.")
    db.save_chunk("d1", 1, 200, 1, "Annex I", "Annex I",
                  "An unrelated paragraph about humidity sensors.")
    return db


def test_three_part_clause_number_is_found(tmp_path):
    db = _seeded_db(tmp_path)
    hits = db.search_fts_chunks("3.4.5", limit=10)
    assert hits, "FTS returned nothing for a clause reference written as N.N.N"
    assert any("3.4.5" in h["chunk_text"] for h in hits)


def test_two_part_number_is_found(tmp_path):
    db = _seeded_db(tmp_path)
    hits = db.search_fts_chunks("6.5", limit=10)
    assert hits, "FTS returned nothing for a decimal such as 6.5"
    assert any("6.5" in h["chunk_text"] for h in hits)


def test_clause_number_inside_chinese_query(tmp_path):
    db = _seeded_db(tmp_path)
    hits = db.search_fts_chunks("第3.4.5条的要求", limit=10)
    assert hits, "FTS returned nothing for a clause number inside a Chinese query"


def test_absent_clause_number_returns_nothing(tmp_path):
    db = _seeded_db(tmp_path)
    assert db.search_fts_chunks("9.9.9", limit=10) == []


def test_plain_text_query_still_matches(tmp_path):
    db = _seeded_db(tmp_path)
    hits = db.search_fts_chunks("humidity sensors", limit=10)
    assert hits, "protecting clause numbers must not disturb ordinary queries"
