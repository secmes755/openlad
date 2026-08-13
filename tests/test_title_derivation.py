"""Title derivation chain pure-logic checks (structured LLM extraction + validation gates)."""
from core.ingestion.builder import DocumentIndexBuilder


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


def _make_builder(client):
    b = object.__new__(DocumentIndexBuilder)  # skip __init__ (no DB/model wiring)
    b.model_client = client
    return b


SUMMARY_ZTE = (
    "本文件为中兴通讯 2024 年年度报告，涵盖重要提示、董事长致辞及财务摘要等核心章节。"
    "公司顺应 5G 成熟与 AI 发展浪潮，推动从连接向算力拓展转型。"
)
CAT = {"category_level1": "General Documents", "category_level2": "Report", "category_level3": ""}


# ---- valid extraction ----
def test_generate_identifiable_title_valid():
    b = _make_builder(_FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"}))
    t = b._generate_identifiable_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t == "中兴通讯 2024 年度报告"


def test_generate_identifiable_title_subject_whitespace_variant():
    # summary is line-broken ("中兴\n通讯") but subject is continuous -> still matches
    b = _make_builder(_FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"}))
    t = b._generate_identifiable_title(
        "2024_年度报告.pdf", "本文件为中兴\n通讯 2024 年年度报告，内容较长。", CAT)
    assert t == "中兴通讯 2024 年度报告"


# ---- anti-hallucination gates ----
def test_hallucinated_subject_dropped_others_survive():
    # "苹果公司" appears nowhere in source -> dropped; year+doc_type pass validation
    b = _make_builder(_FakeModelClient({"subject": "苹果公司", "year": "2024", "doc_type": "年度报告"}))
    t = b._generate_identifiable_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t == "2024 年度报告"


def test_all_fields_hallucinated_returns_none():
    b = _make_builder(_FakeModelClient({"subject": "苹果公司", "year": "1999", "doc_type": "员工手册"}))
    t = b._generate_identifiable_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t is None


# ---- degradation / availability ----
def test_llm_returns_empty_dict_returns_none():
    b = _make_builder(_FakeModelClient({}))
    assert b._generate_identifiable_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


def test_llm_raises_returns_none():
    class Boom:
        def generate_json(self, **kwargs):
            raise RuntimeError("llm down")

    b = _make_builder(Boom())
    assert b._generate_identifiable_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


def test_short_summary_skips_llm_call():
    client = _FakeModelClient({"subject": "X", "year": "2024", "doc_type": "Y"})
    b = _make_builder(client)
    assert b._generate_identifiable_title("a.pdf", "short", CAT) is None
    assert client.calls == []  # no LLM call for tiny/unusable summary


def test_non_dict_llm_output_returns_none():
    b = _make_builder(_FakeModelClient([{"subject": "中兴通讯"}]))  # list is not a dict
    assert b._generate_identifiable_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT) is None


# ---- subject matching ----
def test_subject_in_text_whitespace_insensitive():
    assert DocumentIndexBuilder._subject_in_text("中兴通讯", "本文件为中兴 通讯 2024 年年度报告")
    assert DocumentIndexBuilder._subject_in_text("中兴通讯", SUMMARY_ZTE)
    assert not DocumentIndexBuilder._subject_in_text("苹果", "中兴通讯 2024 年度报告")
    assert not DocumentIndexBuilder._subject_in_text("中", "中兴通讯")  # too short


def test_subject_in_text_empty_inputs():
    assert not DocumentIndexBuilder._subject_in_text("", "abc")
    assert not DocumentIndexBuilder._subject_in_text("abc", "")


# ---- derivation chain priority ----
def test_derive_title_explicit_wins():
    b = _make_builder(_FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"}))
    t = b._derive_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT, explicit_title="自定义标题")
    assert t == "自定义标题"


def test_derive_title_explicit_whitespace_ignored():
    b = _make_builder(_FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"}))
    t = b._derive_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT, explicit_title="   ")
    assert t == "中兴通讯 2024 年度报告"  # blank explicit -> falls through to LLM


def test_derive_title_llm_then_filename_fallback():
    # LLM succeeds -> LLM title
    b = _make_builder(_FakeModelClient({"subject": "中兴通讯", "year": "2024", "doc_type": "年度报告"}))
    assert b._derive_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT) == "中兴通讯 2024 年度报告"

    # LLM fails -> filename-derived title (existing behavior)
    b2 = _make_builder(_FakeModelClient({}))
    t2 = b2._derive_title("2024_年度报告.pdf", SUMMARY_ZTE, CAT)
    assert t2 == "2024 年度报告"  # _extract_title cleans "2024_年度报告.pdf"


def test_derive_title_filename_fallback_when_summary_missing():
    b = _make_builder(_FakeModelClient({"subject": "X", "year": "2024", "doc_type": "Y"}))
    t = b._derive_title("中兴通讯2024年报.pdf", "", CAT)  # empty summary -> LLM skipped
    assert t == "中兴通讯2024年报"  # filename-derived
