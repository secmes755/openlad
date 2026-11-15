"""Pack-declared n-grams make long unsegmented queries searchable."""


def test_search_keywords_include_pack_ngrams():
    from core.text_keywords import extract_search_keywords

    terms = extract_search_keywords("在美的集团的公司中文名称是什么")
    assert "美的集团" in terms
    assert "美的集" in terms
    assert "中文名称" in terms


def test_retriever_uses_search_ngrams_for_fts_terms():
    from core.retrieval.retriever import HierarchicalRetriever

    retriever = object.__new__(HierarchicalRetriever)
    terms = retriever._tokenize_query("在美的集团的公司中文名称是什么")
    assert "美的集团" in terms
    assert "中文名称" in terms
