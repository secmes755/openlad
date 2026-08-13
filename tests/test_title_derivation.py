"""Title derivation chain pure-logic checks (structured LLM extraction + validation gates).

Tests the title_deriver module directly (stdlib-only) so the CI minimal
dependency set (no Pillow/OCR) can run them without importing the builder.
"""
from core.ingestion.title_deriver import (
    derive_title,
    generate_identifiable_title,
    subject_in_text,
)


class _FakeModelClient:
    """Deterministic model client stub: returns a canned JSON dict (or raises)."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate_json(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        self.calls.append(prompt)
        if callable(self.result):
            return self.result(prompt)
        return self.result


SUMMARY_ZTE = (
    "本文件为中兴通讯 2024 年年度报告，涵盖重要提示、董事长致辞及财务摘要等核心章节。"
    "公司顺应 5G 成熟与 AI 发展浪潮，推动从连接向算力拓展转型。"
)
CAT = {"category_level1": "General Documents", "category_level2": "Report", "category_level3": ""}


# ---- valid extraction ----
def test_generate_identifiable_title_valid():
    client = _FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"})
    t = generate_identifiable_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t == "中兴通讯 2024 年度报告"


def test_generate_identifiable_title_subject_whitespace_variant():
    # summary is line-broken ("中兴\n通讯") but subject is continuous -> still matches
    client = _FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"})
    t = generate_identifiable_title(
        client, "2024_年度报告.pdf", "本文件为中兴\n通讯 2024 年年度报告，内容较长。", CAT)
    assert t == "中兴通讯 2024 年度报告"


# ---- anti-hallucination gates ----
def test_hallucinated_subject_dropped_others_survive():
    # "苹果公司" appears nowhere in source -> dropped; year+doc_type pass validation
    client = _FakeModelClient({"subject": "苹果公司", "year": "2024", "doc_type": "年度报告"})
    t = generate_identifiable_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t == "2024 年度报告"


def test_all_fields_hallucinated_returns_none():
    client = _FakeModelClient({"subject": "苹果公司", "year": "1999", "doc_type": "员工手册"})
    t = generate_identifiable_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t is None


# ---- degradation / availability ----
def test_llm_returns_empty_dict_returns_none():
    client = _FakeModelClient({})
    assert generate_identifiable_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


def test_llm_raises_returns_none():
    class Boom:
        def generate_json(self, **kwargs):
            raise RuntimeError("llm down")

    assert generate_identifiable_title(Boom(), "2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


def test_short_summary_skips_llm_call():
    client = _FakeModelClient({"subject": "X", "year": "2024", "doc_type": "Y"})
    assert generate_identifiable_title(client, "a.pdf", "short", CAT) is None
    assert client.calls == []  # no LLM call for tiny/unusable summary


def test_non_dict_llm_output_returns_none():
    client = _FakeModelClient([{"subject": "中兴通讯"}])  # list is not a dict
    assert generate_identifiable_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


# ---- subject matching ----
def test_subject_in_text_whitespace_insensitive():
    assert subject_in_text("中兴通讯", "本文件为中兴 通讯 2024 年年度报告")
    assert subject_in_text("中兴通讯", SUMMARY_ZTE)
    assert not subject_in_text("苹果", "中兴通讯 2024 年度报告")
    assert not subject_in_text("中", "中兴通讯")  # too short


def test_subject_in_text_empty_inputs():
    assert not subject_in_text("", "abc")
    assert not subject_in_text("abc", "")


# ---- derivation chain priority ----
def test_derive_title_explicit_wins():
    client = _FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"})
    t = derive_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT, explicit_title="自定义标题")
    assert t == "自定义标题"


def test_derive_title_explicit_whitespace_ignored():
    client = _FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"})
    t = derive_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT, explicit_title="   ")
    assert t == "中兴通讯 2024 年度报告"  # blank explicit -> falls through to LLM


def test_derive_title_llm_then_none_fallback():
    # LLM succeeds -> LLM title
    client = _FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"})
    assert derive_title(client, "2024_年度报告.pdf", SUMMARY_ZTE, CAT) == "中兴通讯 2024 年度报告"

    # LLM fails -> None (caller falls back to filename-derived title)
    client2 = _FakeModelClient({})
    assert derive_title(client2, "2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


def test_derive_title_llm_skipped_when_summary_missing():
    client = _FakeModelClient({"subject": "X", "year": "2024", "doc_type": "Y"})
    assert derive_title(client, "中兴通讯2024年报.pdf", "", CAT) is None  # empty summary -> None
