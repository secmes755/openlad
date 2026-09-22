"""Report-epoch scoping: when a query pins a year to a report noun, candidate
documents of other epochs are pruned; comparison periods are not epochs."""
from core.retrieval.planner import (
    doc_report_epoch,
    query_report_epochs,
    scope_candidates_by_report_epoch,
)

ZTE_2023 = {"id": "aaaa2023", "title": "中兴通讯2023",
            "filename": "u-u-i-d_2023_年度报告.pdf"}
ZTE_2024 = {"id": "bbbb2024", "title": "中兴通讯2024",
            "filename": "774249c6-c215-445d-ae0e-62f15455c370_2024_年度报告.pdf"}
ZTE_2025 = {"id": "cccc2025", "title": "中兴通讯2025",
            "filename": "89d696c2-0f3d-4358-af29-95817c1c6906_2025_年度报告.pdf"}
DATASHEET = {"id": "dddd0000", "title": "瑞芯微",
             "filename": "rockchip_rk3568.pdf"}
DOCS = [ZTE_2023, ZTE_2024, ZTE_2025, DATASHEET]
ALL_IDS = [d["id"] for d in DOCS]


def test_query_epoch_named_report_noun():
    q = "中兴通讯2025年年度报告显示,公司2025年度净利润较2024年下降了百分之多少?"
    assert query_report_epochs(q) == {"2025"}


def test_query_epoch_no_year_separator():
    assert query_report_epochs("请总结2025年度报告的要点") == {"2025"}


def test_query_epoch_short_forms():
    assert query_report_epochs("2025年报和2024年报对比") == {"2024", "2025"}
    assert query_report_epochs("2025年半年报披露了什么") == {"2025"}
    assert query_report_epochs("2025年第一季度报告") == {"2025"}
    assert query_report_epochs("2025财年报告") == {"2025"}


def test_query_epoch_comparison_period_is_not_epoch():
    assert query_report_epochs("2025年净利润较2024年下降了多少?") == set()
    assert query_report_epochs("2024年营业收入是多少?") == set()
    assert query_report_epochs("") == set()


def test_query_epoch_reporting_period_is_not_document():
    # "报告期末" names a reporting period, not a report document.
    assert query_report_epochs("2025年报告期末应收账款是多少?") == set()
    assert query_report_epochs("截至2025年6月30日") == set()


def test_doc_epoch_from_title_last_year_wins():
    assert doc_report_epoch(ZTE_2025) == "2025"


def test_doc_epoch_filename_fallback_uuid_stripped():
    doc = {"id": "x", "title": "年度报告",
           "filename": "4075e678-0022-4369-8f29-95817c1c6906_2025_年度报告.pdf"}
    assert doc_report_epoch(doc) == "2025"


def test_doc_epoch_none_when_no_year():
    assert doc_report_epoch(DATASHEET) is None
    assert doc_report_epoch(None) is None
    assert doc_report_epoch({"id": "y"}) is None


def test_scoping_prunes_off_epoch_documents():
    q = "中兴通讯2025年年度报告显示,净利润较2024年下降了百分之多少?"
    kept = scope_candidates_by_report_epoch(q, ALL_IDS, DOCS)
    assert ZTE_2025["id"] in kept
    assert DATASHEET["id"] in kept  # epoch-less docs are never pruned
    assert ZTE_2024["id"] not in kept
    assert ZTE_2023["id"] not in kept


def test_scoping_keeps_multiple_named_epochs():
    kept = scope_candidates_by_report_epoch(
        "对比2025年报和2024年报的毛利率", ALL_IDS, DOCS)
    assert ZTE_2025["id"] in kept and ZTE_2024["id"] in kept
    assert ZTE_2023["id"] not in kept


def test_scoping_no_epoch_query_is_noop():
    q = "中兴通讯2025年度净利润较2024年下降了百分之多少?"
    assert scope_candidates_by_report_epoch(q, ALL_IDS, DOCS) == ALL_IDS


def test_scoping_refuses_to_empty_candidate_set():
    # Query names 2022 but no candidate document has that epoch.
    kept = scope_candidates_by_report_epoch(
        "2022年年度报告显示", [ZTE_2024["id"], ZTE_2025["id"]], DOCS)
    assert kept == [ZTE_2024["id"], ZTE_2025["id"]]


def test_scoping_disabled_is_noop():
    q = "中兴通讯2025年年度报告显示,净利润是多少?"
    assert scope_candidates_by_report_epoch(q, ALL_IDS, DOCS, enabled=False) == ALL_IDS


def test_scoping_unknown_candidate_id_is_kept():
    # An id not present in docs cannot be epoch-classified -> keep.
    kept = scope_candidates_by_report_epoch(
        "中兴通讯2025年年度报告显示", ["zzzz9999", ZTE_2024["id"]], DOCS)
    assert kept == ["zzzz9999"]
