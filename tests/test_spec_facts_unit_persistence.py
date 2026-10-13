"""F-12: the ingest builder must persist every field the extractor produces.

The extractor has always emitted a `unit` (and an `extractor` tag), but the
online builder path called `insert_spec_fact` without them, so the DB default
("") silently won for every row — 35,881 facts in the reporting tenant had
unit="" while the offline re-extraction script (scripts/extract_spec_facts.py)
passed them correctly. The online and offline paths must agree: whatever the
extractor returns is what gets stored.

The `unit` column is not read back by retrieval today, so this is a
data-integrity divergence, not a retrieval regression — but the two code paths
disagreeing about their own contract is exactly the kind of silent loss this
suite exists to pin.
"""
from core.ingestion.builder import DocumentIndexBuilder


class _RecordingFacts:
    def __init__(self):
        self.facts = []

    def insert_spec_fact(self, **kwargs):
        self.facts.append(kwargs)


def _builder(monkeypatch, extractor):
    monkeypatch.setattr(
        "core.ingestion.spec_facts_extractor.extract_spec_facts_from_text",
        extractor)
    builder = object.__new__(DocumentIndexBuilder)
    db = _RecordingFacts()
    monkeypatch.setattr(builder, "_get_dbs", lambda tenant_id=None: (db, None))
    return builder, db


def _run(builder, pages, extraction=None):
    return builder._extract_spec_facts("doc-1", pages, "admin", None, None)


def test_unit_is_persisted(monkeypatch):
    """The extractor's unit must reach insert_spec_fact, not fall to the default."""
    def one_fact(text, page_num, entity, doc_id, extraction=None):
        return [{"doc_id": doc_id, "entity": entity, "attribute": "compute power",
                 "value": "16 TOPS", "unit": "TOPS", "page_num": page_num,
                 "source_text": text, "extractor": "rule", "verified": 1}]

    builder, db = _builder(monkeypatch, one_fact)
    _run(builder, [{"page_num": 1, "page_text": "NPU: 16 TOPS"}])

    assert len(db.facts) == 1
    assert db.facts[0]["unit"] == "TOPS", \
        "builder dropped the extractor's unit — the F-12 regression"


def test_extractor_tag_is_persisted(monkeypatch):
    """The extractor tag must also survive the builder call (same omission)."""
    def one_fact(text, page_num, entity, doc_id, extraction=None):
        return [{"doc_id": doc_id, "entity": entity, "attribute": "compute power",
                 "value": "16 TOPS", "unit": "TOPS", "page_num": page_num,
                 "source_text": text, "extractor": "rule", "verified": 1}]

    builder, db = _builder(monkeypatch, one_fact)
    _run(builder, [{"page_num": 1, "page_text": "NPU: 16 TOPS"}])

    assert db.facts[0]["extractor"] == "rule"


def test_missing_unit_defaults_empty(monkeypatch):
    """A fact without a unit key still inserts (backward compatible)."""
    def one_fact(text, page_num, entity, doc_id, extraction=None):
        return [{"doc_id": doc_id, "entity": entity, "attribute": "ball size",
                 "value": "0.35mm", "page_num": page_num,
                 "source_text": text, "verified": 1}]

    builder, db = _builder(monkeypatch, one_fact)
    _run(builder, [{"page_num": 1, "page_text": "ball size: 0.35mm"}])

    assert len(db.facts) == 1
    assert db.facts[0]["unit"] == ""
    assert db.facts[0]["extractor"] == "rule"


def test_frequency_unit_survives(monkeypatch):
    """The frequency pattern's surface-case unit ("GHz") must be stored as-is."""
    def one_fact(text, page_num, entity, doc_id, extraction=None):
        return [{"doc_id": doc_id, "entity": entity,
                 "attribute": "Max CPU frequency", "value": "2 GHz", "unit": "GHz",
                 "page_num": page_num, "source_text": text,
                 "extractor": "rule", "verified": 1}]

    builder, db = _builder(monkeypatch, one_fact)
    _run(builder, [{"page_num": 1, "page_text": "Max CPU frequency NA NA 2 GHz"}])

    assert db.facts[0]["unit"] == "GHz"
