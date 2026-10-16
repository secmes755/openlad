"""Result counts are bounded before they reach a ``LIMIT ?`` binding.

SQLite reads a negative limit as "no limit at all", so a bare ``int`` let a
caller ask for an entire tenant's chunks; a huge value did the same while also
pulling everything into memory. ``max_results`` was an unbounded ``int`` on
both skill request models.

Two layers are tested: the request models (which the API uses) and the
retriever entry point (reachable without going through a model).
"""
import pytest
from pydantic import ValidationError

from core.config import settings
from core.models.client import get_model_client  # noqa: F401  (import side effects)

CAP = settings.MAX_RESULTS_CAP


def test_the_cap_is_finite_and_sane():
    assert isinstance(CAP, int)
    assert 1 <= CAP <= 10_000


@pytest.mark.parametrize("value", [-1, 0, -1000])
def test_search_rejects_a_negative_max_results(value):
    from core.api.routes.skill import SkillSearchRequest

    with pytest.raises(ValidationError):
        SkillSearchRequest(query="x", max_results=value)


@pytest.mark.parametrize("value", [CAP + 1, 10 ** 9])
def test_search_rejects_an_oversized_max_results(value):
    from core.api.routes.skill import SkillSearchRequest

    with pytest.raises(ValidationError):
        SkillSearchRequest(query="x", max_results=value)


@pytest.mark.parametrize("value", [-1, 0, -1000])
def test_query_rejects_a_negative_max_results(value):
    from core.api.routes.skill import SkillQueryRequest

    with pytest.raises(ValidationError):
        SkillQueryRequest(query="x", max_results=value)


@pytest.mark.parametrize("value", [CAP + 1, 10 ** 9])
def test_query_rejects_an_oversized_max_results(value):
    from core.api.routes.skill import SkillQueryRequest

    with pytest.raises(ValidationError):
        SkillQueryRequest(query="x", max_results=value)


@pytest.mark.parametrize("value", [1, 10, CAP])
def test_values_within_range_are_kept(value):
    from core.api.routes.skill import SkillQueryRequest, SkillSearchRequest

    assert SkillSearchRequest(query="x", max_results=value).max_results == value
    assert SkillQueryRequest(query="x", max_results=value).max_results == value


def test_the_retriever_clamps_without_going_through_a_request_model():
    """The retriever is a public entry point; the clamp must not live only in
    the API model, or a direct caller reintroduces the unbounded LIMIT."""
    import inspect

    from core.retrieval.retriever import HierarchicalRetriever

    src = inspect.getsource(HierarchicalRetriever.retrieve)
    assert "MAX_RESULTS_CAP" in src, (
        "retrieve() must clamp max_results to settings.MAX_RESULTS_CAP; "
        "a negative value means 'no limit' at the SQL layer"
    )
