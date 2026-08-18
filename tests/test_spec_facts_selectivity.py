"""Selectivity guard for spec-fact keyword matching (real SQLite, no LLM).

A keyword that touches too many distinct attributes in the fact table is
non-selective (generic verbs like "support" appear in every "Support X"
source line) and must be dropped from scoring; selective keywords keep
working so legitimate injections are unchanged.
"""
import pytest

from core.config import settings
from core.db.tenant_db import TenantMetadataDB
from core.retrieval.spec_facts import lookup_spec_facts

_SUPPORT_FACTS = [
    ("PCIe protocol", "PCIe3.1(8Gbps), PCIe2.1",
     "Support PCIe3.1(8Gbps) protocol and backward compatible with the PCIe2.1 and PCIe1.1"),
    ("USB", "USB3.0", "Support USB3.0 interface"),
    ("Display interface", "BT656/BT1120", "Support BT656/BT1120 interface"),
    ("Camera interface", "MIPI-CSI", "Support MIPI-CSI interface"),
    ("Ethernet", "2x RGMII", "Support two RGMII interfaces"),
    ("PCIe controller count", "2", "Support two PCIe controller with x1 mode"),
]


@pytest.fixture
def db(tmp_path):
    d = TenantMetadataDB(tmp_path / "meta.db")
    for attr, val, src in _SUPPORT_FACTS:
        d.insert_spec_fact("doc1", "RK3568", attr, val, 16, src, verified=1)
    return d


@pytest.fixture
def _restore_config():
    snapshot = dict(settings.CONTEXT_CONFIG)
    yield
    settings.CONTEXT_CONFIG.clear()
    settings.CONTEXT_CONFIG.update(snapshot)


class TestKeywordSpread:
    def test_generic_verb_spans_many_attributes(self, db):
        spread = db.spec_fact_keyword_spread(["support"])
        assert spread["support"] == len(_SUPPORT_FACTS)

    def test_selective_token_confined_to_one_family(self, db):
        spread = db.spec_fact_keyword_spread(["pcie"])
        assert spread["pcie"] == 2  # PCIe protocol + PCIe controller count

    def test_absent_token_has_zero_spread(self, db):
        spread = db.spec_fact_keyword_spread(["bluetooth"])
        assert spread["bluetooth"] == 0


class TestSelectivityGuard:
    def test_generic_verb_query_injects_nothing(self, db, _restore_config):
        settings.CONTEXT_CONFIG["spec_facts_selectivity_guard"] = True
        # "support" is dropped; "bluetooth" matches nothing; the entity token
        # alone is below min_hits -> no injection.
        facts = lookup_spec_facts("RK3568 datasheet bluetooth 5.0 support", db)
        assert facts == []

    def test_selective_query_still_injects(self, db, _restore_config):
        settings.CONTEXT_CONFIG["spec_facts_selectivity_guard"] = True
        facts = lookup_spec_facts("RK3568 PCIe protocol support", db)
        assert facts
        assert all("PCIe" in f["attribute"] for f in facts)

    def test_guard_off_restores_legacy_behavior(self, db, _restore_config):
        settings.CONTEXT_CONFIG["spec_facts_selectivity_guard"] = False
        facts = lookup_spec_facts("RK3568 datasheet bluetooth 5.0 support", db)
        # legacy: entity + "support" qualify every "Support X" fact
        assert len(facts) >= 5

    def test_entity_token_exempt_from_guard(self, db, _restore_config):
        # Entity tokens span every attribute yet must survive (entity
        # restriction handles scoping); a selective second token then
        # qualifies the matching facts.
        settings.CONTEXT_CONFIG["spec_facts_selectivity_guard"] = True
        settings.CONTEXT_CONFIG["spec_facts_selectivity_max_attrs"] = 1
        facts = lookup_spec_facts("RK3568 USB", db)
        assert any(f["attribute"] == "USB" for f in facts)
