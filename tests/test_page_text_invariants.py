"""Page-text invariants: raw_text is never NULL in storage, and
SearchResult.content is always a string.

Regression coverage for the crash where a NULL raw_text page (produced when
text extraction came back empty, e.g. VLM-degraded chart pages) flowed into
retrieval and broke len(r.content) in the chapter-retrieve quota loop.
"""
from core.db.tenant_db import TenantMetadataDB
from core.retrieval.retriever import SearchResult


def test_search_result_content_normalized_to_str():
    assert SearchResult(doc_id="d", content=None).content == ""
    assert SearchResult(doc_id="d", content="x").content == "x"
    # default stays intact
    assert SearchResult(doc_id="d").content == ""


def test_save_page_never_stores_null_raw_text(tmp_path):
    db = TenantMetadataDB(tmp_path / "metadata.db")
    db.save_document("doc1", filename="f.pdf", title="t", status="verified")
    db.save_page("doc1", 1, raw_text=None)
    db.save_page("doc1", 2, raw_text="")
    db.save_page("doc1", 3, raw_text="real text")

    pages = {p["page_num"]: p for p in db.get_document_pages("doc1")}
    assert pages[1]["raw_text"] == ""
    assert pages[2]["raw_text"] == ""
    assert pages[3]["raw_text"] == "real text"
