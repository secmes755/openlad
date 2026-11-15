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
