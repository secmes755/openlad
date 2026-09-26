"""Ingest warnings are categorised: config/declaration notices are not content loss."""
from core.ingestion.builder import DocumentIndexBuilder


def test_declaration_warning_does_not_degrade_document():
    status, metadata = DocumentIndexBuilder._ingest_warning_metadata(
        [], ["declared industry 'semiconductor' but no pack installed"]
    )

    assert status == "verified"
    assert "ingest_warnings" not in metadata
    assert metadata["ingest_warning_categories"]["config"] == [
        "declared industry 'semiconductor' but no pack installed"
    ]


def test_content_warning_degrades_and_stays_in_content_category():
    content = ["pages 2 have an unreadable text layer"]
    config = ["declared industry 'x' but no pack installed"]

    status, metadata = DocumentIndexBuilder._ingest_warning_metadata(content, config)

    assert status == "degraded"
    assert metadata["ingest_warnings"] == content
    assert metadata["ingest_warning_categories"] == {"content": content, "config": config}
