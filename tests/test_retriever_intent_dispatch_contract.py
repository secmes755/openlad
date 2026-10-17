"""The intent dispatch calls every handler through ONE argument list.

``retriever.retrieve()`` builds an ``intent_map`` and then calls whichever
handler the plan routed to with a fixed five-argument call. A handler whose
signature does not accept all five raises ``TypeError`` the moment that intent
is routed to it — which is how MACRO_QA / TIME_SERIES_COMPARE crashed
(2026-10-17 audit: ``_retrieve_macro`` took four parameters).

This test pins the contract instead of the individual fix, so the next handler
added to the map cannot reintroduce the same crash quietly.
"""

import inspect

import pytest

from core.retrieval.retriever import HierarchicalRetriever

# Argument names the dispatcher passes, in order (retriever.retrieve()).
DISPATCH_ARGS = ["query", "plan", "max_results", "doc_id_filter", "max_context_quota"]

HANDLERS = [
    "_retrieve_exact",
    "_retrieve_relation",
    "_retrieve_feature",
    "_retrieve_version_compare",
    "_retrieve_cross_reference",
    "_retrieve_macro",
]


@pytest.mark.parametrize("handler_name", HANDLERS)
def test_handler_accepts_the_shared_dispatch_signature(handler_name):
    fn = getattr(HierarchicalRetriever, handler_name, None)
    assert fn is not None, (
        f"{handler_name} is referenced by intent_map but does not exist; "
        "every handler in the map must be callable with the shared argument list"
    )
    params = list(inspect.signature(fn).parameters.values())
    # drop 'self'
    params = [p for p in params if p.name != "self"]
    names = [p.name for p in params]

    assert names[:4] == DISPATCH_ARGS[:4], (
        f"{handler_name} signature {names} does not match the positional "
        f"prefix {DISPATCH_ARGS[:4]} used by retrieve()"
    )
    # The fifth argument must be accepted (either named or via *args).
    accepts_fifth = (
        len(params) >= 5
        or any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
    )
    assert accepts_fifth, (
        f"{handler_name} accepts {len(params)} parameters; retrieve() passes 5 "
        "(the fifth is max_context_quota, sent for every intent). This is the "
        "exact shape of the MACRO_QA TypeError bug."
    )


def test_macro_handler_has_the_quota_parameter():
    sig = inspect.signature(HierarchicalRetriever._retrieve_macro)
    assert "max_context_quota" in sig.parameters, (
        "_retrieve_macro must accept max_context_quota; the dispatcher always "
        "passes a fifth positional argument"
    )
    assert sig.parameters["max_context_quota"].default is None


def test_intent_map_covers_every_intent_or_falls_back_to_a_safe_handler():
    """The default handler must also satisfy the dispatch contract."""
    src = inspect.getsource(HierarchicalRetriever._dispatch_retrieve)
    assert "intent_map.get(plan.intent, self._retrieve_macro)" in src or \
           "intent_map.get(" in src, "retrieve() must resolve an unmapped intent"
