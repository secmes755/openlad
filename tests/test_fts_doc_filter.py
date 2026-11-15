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
