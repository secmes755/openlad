"""What may populate the spec-fact table: industry vocabulary, not units.

The guard that decides whether a document may be mined for facts accepted a unit
list as vocabulary. The generic pack — which ships SI units (V, mV, A …) for every
industry and no other spec vocabulary at all — therefore opened the table for any
document it was applied to, and the pack-independent colon-header pattern then
read whatever prose was there: on annual reports "Less: Corporate income tax
143,692" became a fact with attribute "Less", and on a PDF whose text layer is
font glyph codes "(cid:6813)" became one with attribute "cid". Measured on a
datasheet corpus, the units-only configuration produced 241 facts on its own.

Units cannot carry that signal: they are what a value is measured *in*, and they
are deliberately industry-neutral. Only vocabulary an industry declares (spec
headers, frequency terms, support objects, codec literals, the compute attribute
name) means "this kind of document has spec facts".
"""
from pathlib import Path

import yaml

from core.ingestion.spec_facts_extractor import extract_spec_facts_from_text

RULES = (Path(__file__).resolve().parent.parent
         / "industries/sample_semiconductor/retrieval/rules.yaml")

# What industries/generic/retrieval/rules.yaml declares: units and nothing else.
GENERIC_EXTRACTION = {"compute_units": ["V", "mV", "kV", "A", "mA", "W", "MHz", "GHz"]}

STATEMENT_LINES = (
    "Less: Corporate income tax 143,692 103,624 668,578\n"
    "Including: Interest\n"
    "Unit: RMB\n"
    "Plus: Other income 54,179,605.39\n"
)
SPEC_LINES = "GPU\n Mali-G52 1-Core-2EE\n"


def _pack_extraction() -> dict:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))["spec_extraction"]


def test_a_unit_list_alone_does_not_open_the_fact_table():
    facts = extract_spec_facts_from_text(
        STATEMENT_LINES, 1, "acme", "doc-1", extraction=GENERIC_EXTRACTION)
    assert facts == [], f"statement lines became facts: {facts}"


def test_no_configuration_at_all_opens_nothing():
    assert extract_spec_facts_from_text(STATEMENT_LINES, 1, "acme", "doc-1") == []


def test_the_pack_that_declares_vocabulary_still_extracts():
    facts = extract_spec_facts_from_text(
        SPEC_LINES, 1, "RK3588", "doc-1", extraction=_pack_extraction())

    assert facts, "the semiconductor pack's declared vocabulary must keep working"
    assert [f["attribute"] for f in facts] == ["GPU"]
    assert facts[0]["value"].startswith("Mali")
