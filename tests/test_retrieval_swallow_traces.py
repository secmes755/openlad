"""Industry-pack failures must leave a trace.

Handlers in the retrieval layer swallowed exceptions and continued with a
degraded result: the pack's query vocabulary, its retrieval rules, its spec
terms. From the outside that looked exactly like a pack which simply had nothing
to contribute, so a broken pack made answers quietly worse. Each site now logs —
at warning where the loss affects answer quality, at debug where the path is
already degraded or genuinely best-effort.
"""
import logging

from core.retrieval.executor import RetrievalExecutor
from core.retrieval.retriever import HierarchicalRetriever


def _boom(*args, **kwargs):
    raise RuntimeError("pack registry exploded")


def test_executor_reports_unavailable_pack_vocabulary(monkeypatch, caplog):
    monkeypatch.setattr("core.plugins.get_plugin_registry", _boom)
    executor = RetrievalExecutor(tenant_id="admin")

    with caplog.at_level(logging.WARNING):
        assert executor._get_query_expansion_keywords("auto") == []

    assert any("query expansion keywords unavailable" in r.message for r in caplog.records), \
        "losing the pack vocabulary must not be silent"


def test_retriever_reports_unloadable_industry_rules(monkeypatch, caplog):
    monkeypatch.setattr("core.plugins.get_plugin_registry", _boom)
    retriever = HierarchicalRetriever(tenant_id="admin")

    with caplog.at_level(logging.WARNING):
        assert retriever._load_industry_boost_rules_for_retrieval() == {}

    assert any("failed to load industry retrieval rules" in r.message for r in caplog.records), \
        "losing every retrieval rule degrades answers and must be reported"


def test_retriever_reports_failed_industry_detection(monkeypatch, caplog):
    class _FailingRegistry:
        def detect_plugin_for_text(self, query):
            raise RuntimeError("detector exploded")

    monkeypatch.setattr("core.plugins.get_plugin_registry", lambda: _FailingRegistry())
    retriever = HierarchicalRetriever(tenant_id="admin")

    with caplog.at_level(logging.WARNING):
        keywords = retriever._expand_query_terms(["voltage"], "what is the voltage")

    assert keywords == ["voltage"], "the query must still be usable"
    assert any("industry detection failed" in r.message for r in caplog.records), \
        "a pack that cannot be consulted is not the same as a pack with no match"
