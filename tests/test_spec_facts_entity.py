"""The entity dimension of the fact index must mean something or be absent.

``infer_doc_entity`` used to fall back to the cleaned title/filename when no
pack pattern matched, so a document whose name carries no entity produced facts
labelled with a document name — ``<uuid>_2024``, ``..._Roc``. Two things went
wrong with that. The label was rendered to the model as if it named an entity
(``[<uuid>_2024]`` above verbatim facts), and the labels entered the entity
vocabulary that scopes fact injection: the guard compares query tokens against
that vocabulary, so a vocabulary of document labels can never match a real query
entity — the scoping looked active while it could not scope anything.

An unknown entity is now empty. The vocabulary query already excludes empty
entities, so an unknown entity simply stops being a fake one.
"""
from core.db.tenant_db import TenantMetadataDB
from core.ingestion.spec_facts_extractor import infer_doc_entity


def test_an_unknown_entity_is_empty_rather_than_a_document_label():
    assert infer_doc_entity("", "7c926486-d949-4710-b515-7970b76a6bc3_Roc.pdf") == ""
    assert infer_doc_entity("No model here") == ""


def test_a_matching_title_yields_the_entity_the_pack_describes():
    patterns = [r"(RK\d{4})"]
    assert infer_doc_entity("Rockchip RK3588 datasheet", filename="scan-0001.pdf",
                            entity_patterns=patterns) == "RK3588"


def test_the_filename_is_still_matched_when_the_title_is_empty():
    assert infer_doc_entity("", "RK3588 Datasheet.pdf",
                            entity_patterns=[r"(RK\d{4})"]) == "RK3588"


def test_unknown_entities_stay_out_of_the_entity_vocabulary(tmp_path):
    """The vocabulary scopes fact injection, so a document label must not enter it."""
    db = TenantMetadataDB(tmp_path / "metadata.db")
    db.insert_spec_fact(doc_id="d1", entity="", attribute="voltage", value="3.3",
                        page_num=1, source_text="VDD 3.3V")
    db.insert_spec_fact(doc_id="d1", entity="RK3588", attribute="voltage", value="3.3",
                        page_num=1, source_text="VDD 3.3V")

    assert db.get_spec_fact_entities() == ["RK3588"]
