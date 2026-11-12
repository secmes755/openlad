"""Agentic citations are filename-complete, page-complete and page-deduplicated."""
import sqlite3
from types import SimpleNamespace

from core.retrieval.agentic_retriever import AgenticRetriever


def test_vector_page_ids_are_resolved_in_one_batch():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE doc_pages (id INTEGER, page_num INTEGER)')
    conn.executemany('INSERT INTO doc_pages VALUES (?, ?)', [(7, 42), (8, 43)])
    agent = AgenticRetriever.__new__(AgenticRetriever)
    agent.metadata_db = SimpleNamespace(get_connection=lambda: conn)
    results = [{'page_id': 7}, {'page_id': 8, 'page_num': 99}]

    agent._attach_page_nums(results)

    assert results == [{'page_id': 7, 'page_num': 42}, {'page_id': 8, 'page_num': 99}]


def test_hybrid_search_merges_by_page_and_keeps_vector_text(monkeypatch):
    agent = AgenticRetriever.__new__(AgenticRetriever)
    monkeypatch.setattr(agent, '_fts_search', lambda *args, **kwargs: [
        {'page_id': 7, 'page_num': 42, 'score': 1.0, 'source': 'fts'},
    ])
    monkeypatch.setattr(agent, '_semantic_search', lambda *args, **kwargs: [
        (0.8, {'page_id': 7, 'page_num': 42, 'chunk_idx': 2, 'chunk_text': 'supporting chunk'}),
        (0.7, {'page_id': 7, 'page_num': 42, 'chunk_idx': 3, 'chunk_text': 'duplicate page'}),
        (0.6, {'page_id': 8, 'page_num': 43, 'chunk_idx': 0, 'chunk_text': 'new page'}),
    ])

    results = agent._hybrid_search('query')

    assert [(r['page_id'], r['page_num']) for r in results] == [(8, 43), (7, 42)]
    assert results[1]['chunk_text'] == 'supporting chunk'


def test_final_sources_aggregate_pages_and_fill_filename():
    sources = AgenticRetriever._merge_sources([
        {'doc_id': 'doc', 'title': 'Document', 'pages': [3, 1]},
        {'doc_id': 'doc', 'filename': 'manual.pdf', 'pages': [2, 1]},
    ])

    assert sources == [{
        'doc_id': 'doc', 'title': 'Document', 'filename': 'manual.pdf',
        'pages': [1, 2, 3],
    }]


def test_single_document_sources_include_filename(monkeypatch):
    agent = AgenticRetriever.__new__(AgenticRetriever)
    agent.config = {}
    agent.spec_facts_plan = {}
    agent.metadata_db = SimpleNamespace(
        get_document=lambda doc_id: {'filename': 'manual.pdf'},
    )
    agent.model_client = SimpleNamespace(generate=lambda *args, **kwargs: 'evidence ' * 30)
    monkeypatch.setattr(agent, '_expand_keywords', lambda *args: [])
    monkeypatch.setattr(agent, '_hybrid_search', lambda *args, **kwargs: [
        {'page_id': 7, 'page_num': 42, 'section_title': 'Facts', 'chunk_text': 'fact'},
    ])
    monkeypatch.setattr(agent, '_read_pages', lambda *args: [
        {'page_num': 42, 'section_title': 'Facts', 'content': 'fact'},
    ])

    result = agent._query_single_document('query', 'doc', 'Document')

    assert result['sources'][0]['filename'] == 'manual.pdf'
    assert result['sources'][0]['pages'] == [42]
