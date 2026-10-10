"""Uploaded files must be recorded under the user's original filename.

Regression suite for the batch-ingestion field report (finding F-02): the
upload route stores files on disk as "{doc_id}_{original}" to keep disk names
unique, but that storage prefix used to leak into ``documents.filename`` —
GET /documents showed prefixed names and offered no way to look a document up
by the name the user actually uploaded.

Contract under test:
  * strip_doc_id_prefix removes a full "{uuid}_" prefix and nothing else
  * DocumentParser.parse records the stripped filename (all dispatch arms
    funnel through parse())
  * TenantMetadataDB list/count accept an exact-match filename filter
"""
import uuid

from core.db.tenant_db import TenantMetadataDB
from core.ingestion.parser import DocumentParser, strip_doc_id_prefix


class TestStripDocIdPrefix:
    def test_strips_full_uuid_prefix(self):
        doc_id = str(uuid.uuid4())
        assert strip_doc_id_prefix(f"{doc_id}_p600_datasheet.pdf") == "p600_datasheet.pdf"

    def test_plain_name_unchanged(self):
        assert strip_doc_id_prefix("p600_datasheet.pdf") == "p600_datasheet.pdf"

    def test_partial_uuid_unchanged(self):
        # 8-4-4-4 hex but a short tail — not a full UUID, must not be stripped
        name = "3fa9c812-ab12-4cd5-9e6f_notes.txt"
        assert strip_doc_id_prefix(name) == name

    def test_uuid_without_underscore_unchanged(self):
        name = f"{uuid.uuid4()}report.pdf"
        assert strip_doc_id_prefix(name) == name

    def test_empty_and_none(self):
        assert strip_doc_id_prefix("") == ""
        assert strip_doc_id_prefix(None) == ""


class TestParseRecordsOriginalFilename:
    def test_txt_parse_strips_prefix(self, tmp_path):
        f = tmp_path / f"{uuid.uuid4()}_field_notes.txt"
        f.write_text("hello world", encoding="utf-8")
        doc = DocumentParser().parse(str(f))
        assert doc.filename == "field_notes.txt"

    def test_plain_filename_passes_through(self, tmp_path):
        f = tmp_path / "field_notes.txt"
        f.write_text("hello world", encoding="utf-8")
        doc = DocumentParser().parse(str(f))
        assert doc.filename == "field_notes.txt"


class TestFilenameFilter:
    def _seed(self, tmp_path):
        db = TenantMetadataDB(tmp_path / "metadata.db")
        db.save_document("a" * 32, filename="p600_datasheet.pdf",
                         original_path="/x/p600.pdf", status="verified")
        db.save_document("b" * 32, filename="t536_datasheet.pdf",
                         original_path="/x/t536.pdf", status="verified")
        return db

    def test_list_filters_by_exact_filename(self, tmp_path):
        db = self._seed(tmp_path)
        docs = db.list_documents(filename="p600_datasheet.pdf")
        assert [d["filename"] for d in docs] == ["p600_datasheet.pdf"]

    def test_count_matches_list(self, tmp_path):
        db = self._seed(tmp_path)
        assert db.count_documents() == 2
        assert db.count_documents(filename="p600_datasheet.pdf") == 1
        assert db.count_documents(filename="missing.pdf") == 0

    def test_no_filter_returns_all(self, tmp_path):
        db = self._seed(tmp_path)
        assert len(db.list_documents()) == 2
