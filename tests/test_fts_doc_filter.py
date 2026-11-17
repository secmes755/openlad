"""Document-scoped FTS must rank and limit inside the requested document."""
from core.db.tenant_db import TenantMetadataDB


def test_document_filter_is_applied_before_limit(tmp_path):
    db = TenantMetadataDB(tmp_path / "meta.db")
    for index in range(20):
        db.save_chunk("d2", index + 1, index + 1, 0, "", "",
                      "common token")
    db.save_chunk("d1", 1, 1, 0, "", "",
                  "common token " + "supporting text " * 100)

    hits = db.search_fts_chunks("common", limit=1, doc_id_filter={"d1"})
    assert len(hits) == 1
    assert hits[0]["doc_id"] == "d1"


def test_strict_synonym_search_does_not_add_or_matches(tmp_path):
    db = TenantMetadataDB(str(tmp_path / "strict.db"))
    db.save_document("d1", filename="d1.txt")
    db.save_document("d2", filename="d2.txt")
    db.save_chunk("d1", 1, 1, 0, "", "", "total revenue 100")
    db.save_chunk("d2", 2, 1, 0, "", "", "total assets")
    rows = db.search_fts_chunks("total revenue", strict=True)
    assert [row["doc_id"] for row in rows] == ["d1"]


def test_strict_mode_still_finds_chinese_terms(tmp_path):
    """Chinese terms are decomposed into bigrams, which only LIKE can match.

    Excluding that path under `strict` made every Chinese query return nothing.
    """
    db = TenantMetadataDB(str(tmp_path / "cjk.db"))
    db.save_document("d1", filename="d1.txt")
    db.save_chunk("d1", 1, 1, 0, "", "", "归属于上市公司股东的净利润为一百万元")

    for term in ("净利润", "归属于上市公司股东的净利润", "上市公司"):
        strict = db.search_fts_chunks(term, limit=10, doc_id_filter={"d1"},
                                      strict=True)
        assert strict, f"{term} returned nothing under strict mode"
        assert strict[0]["doc_id"] == "d1"


def test_strict_mode_still_suppresses_or_supplement(tmp_path):
    """strict must keep its original job: no broad OR padding."""
    db = TenantMetadataDB(str(tmp_path / "or.db"))
    db.save_document("d1", filename="d1.txt")
    db.save_chunk("d1", 1, 1, 0, "", "", "operating revenue grew")
    db.save_chunk("d1", 2, 2, 0, "", "", "net profit attributable declined")

    query = "operating profit"
    strict = db.search_fts_chunks(query, limit=10, doc_id_filter={"d1"},
                                  strict=True)
    loose = db.search_fts_chunks(query, limit=10, doc_id_filter={"d1"},
                                 strict=False)
    assert len(strict) <= len(loose)
