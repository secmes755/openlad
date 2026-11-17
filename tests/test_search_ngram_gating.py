"""n-grams exist for Chinese queries that the keyword extractor cannot split.

Chinese is written without spaces, so a whole question can come back as a single
long "word" that no index matches. Substring n-grams are what make it findable.
The same explosion is pure noise when the extractor already produced short
terms, where it competes for the term budget and displaces pack vocabulary.
"""
from core.text_keywords import (
    extract_keywords,
    extract_search_keywords,
    needs_search_ngrams,
    split_search_keywords,
)


def test_unsegmented_chinese_query_needs_ngrams():
    keywords = extract_keywords("在美的集团的公司中文名称是什么")
    assert len(keywords) == 1 and len(keywords[0]) > 4
    assert needs_search_ngrams(keywords)


def test_punctuated_query_with_short_terms_does_not_need_ngrams():
    keywords = extract_keywords(
        "美的集团2023年年度报告中，归属于上市公司股东的净利润是多少？")
    assert any(len(t) <= 4 for t in keywords)
    assert not needs_search_ngrams(keywords)


def test_ngrams_are_generated_only_for_the_unsegmented_case():
    _, derived = split_search_keywords("在美的集团的公司中文名称是什么", 64)
    assert derived, "long CJK question must still produce n-grams"

    keywords, derived = split_search_keywords(
        "美的集团2023年年度报告中，归属于上市公司股东的净利润是多少？", 64)
    assert derived == []
    assert keywords


def test_searchable_ngram_still_locates_the_company_name():
    """The mechanism must keep doing its job after the gating change."""
    terms = extract_search_keywords("在美的集团的公司中文名称是什么")
    assert "美的集" in terms or "的集团" in terms


def test_mixed_script_query_does_not_need_ngrams():
    keywords = extract_keywords("A523 处理器的 GPIO 有几组")
    assert not needs_search_ngrams(keywords)


def test_empty_input_is_not_an_ngram_case():
    assert not needs_search_ngrams([])
