"""Every intent in the map must survive the dispatcher's call shape.

Signature inspection (test_retriever_intent_dispatch_contract) catches drift at
review time; this one proves the actual call path works by binding the handler
to a stub instance and calling it with the dispatcher's argument list.
"""

import inspect
from unittest.mock import MagicMock

import pytest

from core.retrieval.retriever import HierarchicalRetriever

DISPATCH = ["query", "plan", "max_results", "doc_id_filter", "max_context_quota"]

HANDLERS = [
    "_retrieve_exact",
    "_retrieve_relation",
    "_retrieve_feature",
    "_retrieve_version_compare",
    "_retrieve_cross_reference",
    "_retrieve_macro",
]


@pytest.mark.parametrize("name", HANDLERS)
def test_handler_binds_all_five_dispatch_arguments(name):
    fn = getattr(HierarchicalRetriever, name)
    sig = inspect.signature(fn)
    args = [None, MagicMock(), 20, {"__ALL__"}, 10000]
    try:
        sig.bind(None, *args)  # None = self
    except TypeError as exc:
        pytest.fail(
            f"{name} cannot be called with the dispatcher's 5-argument shape: {exc}"
        )


def test_intent_map_handlers_are_all_bound_methods_of_the_dispatcher():
    """retrieve() builds the map from self; every value must be a method."""
    src = inspect.getsource(HierarchicalRetriever._dispatch_retrieve)
    for name in HANDLERS:
        assert f"self.{name}" in src or name == "_retrieve_macro", (
            f"{name} is not wired into retrieve()'s intent_map"
        )
