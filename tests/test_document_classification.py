"""Document typing must not force an unseen document into a known category, and an
inferred category must never override the industry declared on upload.

Regression coverage for the ingestion defect where every semiconductor datasheet
was classified as 财务公告 and then processed with financial-pack vocabulary. The
pack shipped shared/taxonomy.yaml but never exposed it, the candidate list was
built from taxonomy.yaml alone (so that pack was invisible), and the prompt
forbade "Other" — leaving a financial category as the only usable answer.
"""
import pytest

from core.ingestion.builder import _may_route_by_category
from core.ingestion.classifier import DocumentClassifier
from core.plugins import get_plugin_registry
from core.retrieval.planner import _routing_category


class _FakeModelClient:
    """Stands in for the LLM: a canned JSON answer, or a failure."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def generate_json(self, prompt, system_prompt=None, temperature=0.3, max_tokens=1024):
        if self.error:
            raise self.error
        return self.payload


def _classifier(payload=None, error=None) -> DocumentClassifier:
    classifier = DocumentClassifier()
    classifier.model_client = _FakeModelClient(payload, error)
    return classifier


def test_sample_pack_exposes_its_own_taxonomy():
    """The pack ships shared/taxonomy.yaml; the classifier can only use it if the
    pack publishes it (the base class defaults to {})."""
    pack = get_plugin_registry().get_plugin("sample_semiconductor")
    assert pack is not None
    taxonomy = pack.taxonomy

    assert taxonomy, "semiconductor pack must publish its taxonomy"
    subcategories = [item.get("name") for item in taxonomy.get("level2", [])]
    assert "数据手册" in subcategories


def test_candidate_list_includes_every_routable_pack():
    """Candidates must be the packs' routing keys (taxonomy names + manifest
    category_mapping) — the same strings the pack resolver matches on."""
    prompt = _classifier()._build_taxonomy_prompt()

    assert "数据手册" in prompt, "taxonomy names of a Python pack must be offered"
    assert "Datasheets" in prompt, "manifest category_mapping must be offered"
    assert "does not fit any category" in prompt, "unknown must be an allowed answer"


@pytest.mark.parametrize("answer", ["Other", "未知", "未分类", "", "  ", None, "unclassified"])
def test_unknown_answers_are_stored_as_null(answer):
    """Unknown is NULL, never a placeholder label: downstream code routes on
    these fields, so a literal "Other" would read as a real category."""
    result = _classifier({"category_level1": answer, "category_level2": answer,
                          "category_level3": answer, "confidence": 0.9}).classify(
        "unknown.pdf", "unknown", "irrelevant")

    assert result["category_level1"] is None
    assert result["category_level2"] is None
    assert result["category_level3"] is None
    assert result["unknown"] is True


def test_levels_below_a_missing_level_are_not_fabricated():
    """The old code filled level3 with a filename-derived product code, which then
    read as a real classification."""
    result = _classifier({"category_level1": "技术与产品文档", "category_level2": "",
                          "category_level3": "RK3568", "confidence": 0.8}).classify(
        "RK3568_Datasheet.pdf", "RK3568", "sample")

    assert result["category_level1"] == "技术与产品文档"
    assert result["category_level2"] is None
    assert result["category_level3"] is None, "no level below a missing level"
    assert result["unknown"] is False


def test_no_usable_answer_reports_unknown_instead_of_a_guessed_type():
    """When the model fails outright the old code stored a hardcoded
    'Technical Documentation / Datasheet' guess — indistinguishable from a real
    answer, and it selected an extraction pack."""
    for payload, error in ((None, None), ({"broken": True}, None), (None, RuntimeError("boom"))):
        result = _classifier(payload, error).classify(
            "RK3568_Datasheet.pdf", "RK3568", "sample")
        assert result["category_level1"] is None
        assert result["unknown"] is True
        assert result["confidence"] == 0.0


def test_declared_industry_resolves_through_alias():
    """Callers declare "semiconductor"; the pack id is "sample_semiconductor".
    Without alias resolution the declaration silently failed to bind."""
    registry = get_plugin_registry()

    assert registry.get_plugin("sample_semiconductor") is not None
    assert registry.get_plugin("semiconductor") is not None
    assert registry.get_plugin("半导体") is not None
    assert registry.get_plugin("no_such_pack") is None


def test_declared_industry_wins_over_inferred_category():
    """The inferred label used to shadow the declaration in query routing."""
    doc = {"industry_package_id": "semiconductor", "category_level1": "财务报告",
           "category_level2": "财务公告"}

    assert _routing_category(doc) == "semiconductor"


def test_routing_category_falls_back_neutral_for_untyped_documents():
    assert _routing_category({}) == "general"
    assert _routing_category({"category_level1": None,
                              "industry_package_id": None}) == "general"
    assert _routing_category({"category_level1": "技术与产品文档"}) == "技术与产品文档"


def test_inferred_category_may_only_route_when_classification_is_trustworthy():
    """A guess must not pick the extraction vocabulary: unknown, low confidence and
    an unresolvable declaration all disable category-based routing."""
    confident = {"category_level1": "技术与产品文档", "confidence": 0.9, "unknown": False}

    assert _may_route_by_category(confident, []) is True
    assert _may_route_by_category({**confident, "unknown": True}, []) is False
    assert _may_route_by_category({**confident, "confidence": 0.2}, []) is False
    assert _may_route_by_category({**confident, "confidence": None}, []) is False
    assert _may_route_by_category({**confident, "confidence": "high"}, []) is False
    assert _may_route_by_category(confident, ["declared industry 'nope' matches no pack"]) is False
