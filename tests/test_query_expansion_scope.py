"""Query expansion must use the selected or detected pack, never every pack."""
from types import SimpleNamespace

from core.retrieval.executor import RetrievalExecutor
from core.retrieval.retriever import SearchResult, SegmentMerger


class _Registry:
    def __init__(self):
        self.plugins = {
            'finance': SimpleNamespace(
                retrieval=SimpleNamespace(get_query_expansion_keywords=lambda: ['revenue', 'profit'])
            ),
            'semiconductor': SimpleNamespace(
                retrieval=SimpleNamespace(get_query_expansion_keywords=lambda: ['ISP', 'pinout'])
            ),
        }

    def list_plugins(self):
        return list(self.plugins)

    def get_plugin(self, pack_id):
        return self.plugins.get(pack_id)

    def get_plugin_by_category(self, category):
        return self.plugins.get(category)

    def detect_plugin_for_text(self, query):
        if 'chip' in query.lower() or 'rk' in query.lower():
            return self.plugins['semiconductor']
        if 'annual report' in query.lower():
            return self.plugins['finance']
        return None


def test_auto_expansion_does_not_mix_unrelated_installed_packs(monkeypatch):
    monkeypatch.setattr('core.plugins.get_plugin_registry', lambda: _Registry())
    executor = RetrievalExecutor(tenant_id='admin')

    assert executor._get_query_expansion_keywords('auto', query='an otherwise unmatched question') == []


def test_auto_expansion_uses_only_the_detected_pack(monkeypatch):
    monkeypatch.setattr('core.plugins.get_plugin_registry', lambda: _Registry())
    executor = RetrievalExecutor(tenant_id='admin')

    assert executor._get_query_expansion_keywords('auto', query='chip overview') == ['ISP', 'pinout']
    assert executor._get_query_expansion_keywords('auto', query='annual report summary') == ['revenue', 'profit']


def test_explicit_expansion_pack_is_unchanged(monkeypatch):
    monkeypatch.setattr('core.plugins.get_plugin_registry', lambda: _Registry())
    executor = RetrievalExecutor(tenant_id='admin')

    assert executor._get_query_expansion_keywords('semiconductor', query='annual report') == ['ISP', 'pinout']


def test_invalid_explicit_pack_does_not_fall_back_to_all_packs(monkeypatch):
    monkeypatch.setattr('core.plugins.get_plugin_registry', lambda: _Registry())
    executor = RetrievalExecutor(tenant_id='admin')

    assert executor._get_query_expansion_keywords('missing', query='chip overview') == []


def _decomposed_step_query(monkeypatch, subquery, industry='auto'):
    monkeypatch.setattr('core.plugins.get_plugin_registry', lambda: _Registry())
    monkeypatch.setattr('core.retrieval.executor.get_model_client', lambda: SimpleNamespace())
    executor = RetrievalExecutor(tenant_id='admin')
    executor.max_chars = 60000
    monkeypatch.setattr(executor, 'merger', SimpleNamespace(merge=lambda *args, **kwargs: ('', [])))
    monkeypatch.setattr(executor, '_calculate_step_quotas', lambda steps: [20000])
    monkeypatch.setattr(executor, '_resolve_doc_filter', lambda terms, silent=False: terms)
    monkeypatch.setattr(executor, '_match_subquery_to_docs', lambda query, terms: terms)
    executed = []

    def capture_step(tool, query, *args, **kwargs):
        executed.append(query)
        return []

    monkeypatch.setattr(executor, '_execute_step', capture_step)
    executor._execute_decomposed(
        [{'tool': 'decomposed_retrieve', 'query': [subquery], 'doc_filter': []}],
        original_query=subquery,
        industry_hint=industry,
    )
    assert len(executed) == 1
    return executed[0]


def test_decomposed_auto_keeps_unmatched_cjk_subquery_verbatim(monkeypatch):
    assert _decomposed_step_query(monkeypatch, '营业收入') == '营业收入'


def test_decomposed_auto_expands_only_the_detected_pack(monkeypatch):
    query = _decomposed_step_query(monkeypatch, 'RK3568')
    assert query.startswith('RK3568')
    assert 'ISP' in query
    assert 'revenue' not in query


def test_direct_page_evidence_precedes_chapter_context_without_duplicates():
    chapter = [
        SearchResult('doc', page_num=40, content='chapter start'),
        SearchResult('doc', page_num=42, content='full target page'),
    ]
    direct = [
        SearchResult('doc', page_num=42, content='FTS target page'),
        SearchResult('doc', page_num=15, content='related table'),
    ]

    merged = RetrievalExecutor._merge_chapter_with_fts(chapter, direct, direct_score=20.0)

    assert [r.page_num for r in merged] == [42, 15, 40]
    assert merged[0].content == 'full target page'
    assert [r.score for r in merged] == [20.0, 20.0, 0.0]


def test_direct_evidence_survives_final_merger_budget(monkeypatch):
    chapter = [
        SearchResult('doc', page_num=page, score=10.0, content='bulk context\n' * 80,
                     section_title='Broad chapter', title='Document')
        for page in (10, 11, 12)
    ]
    direct = [SearchResult('doc', page_num=42, score=6.4,
                           content='TARGET DIRECT EVIDENCE', title='Document')]
    merged = RetrievalExecutor._merge_chapter_with_fts(
        chapter, direct, direct_score=20.0,
    )

    merger = SegmentMerger()
    monkeypatch.setattr(merger, 'metadata_db', SimpleNamespace(
        get_document=lambda doc_id: {'title': 'Document', 'filename': 'doc.pdf'},
        execute=lambda *args: SimpleNamespace(fetchall=lambda: []),
    ))
    context, _ = merger.merge(
        merged, max_context_chars=500, query='target', industry_hint='auto',
    )

    assert 'TARGET DIRECT EVIDENCE' in context
