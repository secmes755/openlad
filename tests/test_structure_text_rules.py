"""Structure-index text-rule hygiene tests.

Covers the bookmarkless-document structure index defect fixes:
  Fix 1: coverage invariant (no page may be unreachable by section ranges)
  Fix 2: repeated-line blacklist + R2 chapter-heading fallback
  Fix 3: numeric-heading junk filter (corrected <1 alphabetic-word threshold)
  Fix 4: (doc_id, section_path, start_page) composite identity + consumer

The pure functions live in core.ingestion.structure_text_rules (stdlib
only) so these tests run in the CI minimal-dependency environment.
"""
from core.ingestion.structure_text_rules import (
    build_repeated_lines,
    chapter_heading_title_is_valid,
    compose_parent_chapter,
    coverage_invariant_holds,
    ensure_page_coverage,
    numeric_title_is_heading,
)

# ---- Fix 3: numeric-heading acceptance table ----

def test_numeric_heading_must_pass_examples():
    """必过样例：真实章节标题不许误杀。"""
    assert numeric_title_is_heading("1.1", "Overview")
    assert numeric_title_is_heading("4.2", "Revenues and Costs")
    assert numeric_title_is_heading("2.2", "2023 Results")
    assert numeric_title_is_heading("3.1", "2024 Outlook")
    assert numeric_title_is_heading("40", "营业收入和营业成本")


def test_numeric_heading_junk_rejected():
    """§1 垃圾样本：表格行 / 年份序列 / 单元格尾巴必须拒绝。"""
    assert not numeric_title_is_heading("2023", "2022 2023 2022")
    assert not numeric_title_is_heading("147", "9,946,276 N/A 0.14%")
    assert not numeric_title_is_heading("26", "March 2024 ————————")
    assert not numeric_title_is_heading("147", "9,946,276")
    assert not numeric_title_is_heading("0.14", "%")


def test_numeric_heading_single_word_threshold():
    """a 阈值修正：单字母词元标题必须通过（拒绝阈值是 <1，不是 <2）。"""
    assert numeric_title_is_heading("1", "Overview")
    assert numeric_title_is_heading("2", "Results")


def test_numeric_heading_empty_title_rejected():
    assert not numeric_title_is_heading("1", "")


def test_numeric_heading_sentence_fragment_rejected():
    # Fix 3e：正文句子片段（"was even larger. The data..."）必须拒绝
    assert not numeric_title_is_heading(
        "300",
        "billion, and the scale of the elevator industry was even larger. "
        "The data from EqualOcean",
    )


# ---- 继承机制（任务书外发现：chapter_titles 拼接放大垃圾）----

def test_compose_parent_chapter_never_double_prefixes():
    # 已含单位前缀的 title 原样返回（页眉拼接残留 "Chapter 5" 不再变 "Chapter 5 Chapter 5"）
    assert compose_parent_chapter(5, "Chapter 5") == "Chapter 5"
    assert compose_parent_chapter(3, "Section 2") == "Section 2"
    assert compose_parent_chapter(7, "Part IV") == "Part IV"
    # 正常 title 拼一次前缀
    assert compose_parent_chapter(5, "Financial Statements") == "Chapter 5 Financial Statements"


def test_toc_title_stitch_line_rejected_by_validator():
    # TOC 页眉拼接行：chapter_heading_title_is_valid 用于 TOC 提取的拒绝
    assert not chapter_heading_title_is_valid("Chapter 5 128")
    assert not chapter_heading_title_is_valid("Chapter 5")
    assert chapter_heading_title_is_valid("Financial Statements")


# ---- Fix 2b: chapter-heading fallback ----

def test_chapter_heading_title_validity():
    assert chapter_heading_title_is_valid("Introduction")
    assert chapter_heading_title_is_valid("Notes to Financial Statements")
    assert not chapter_heading_title_is_valid("")
    assert not chapter_heading_title_is_valid("Chapter 5")   # 页眉拼接行
    assert not chapter_heading_title_is_valid("Section 2")   # 页眉拼接行
    assert not chapter_heading_title_is_valid("Part III")    # 页眉拼接行


# ---- Fix 2a: repeated-line blacklist ----

def test_repeated_lines_detects_running_headers():
    pages = {
        1: {"page_text": "Chapter 5 Annual Report\nSome content here\nFooter Line"},
        2: {"page_text": "Chapter 5 Annual Report\nMore content here\nFooter Line"},
        3: {"page_text": "Chapter 5 Annual Report\nEven more content\nFooter Line"},
    }
    repeated = build_repeated_lines(pages)
    assert "Chapter 5 Annual Report" in repeated
    assert "Footer Line" in repeated
    assert "Some content here" not in repeated  # 单页内容不进黑名单
    assert "More content here" not in repeated


def test_repeated_lines_skips_short_and_two_page_lines():
    pages = {1: {"page_text": "147\nabcde"}, 2: {"page_text": "147\nabcde"}}
    # 长度 <= 4 的行（147）不参与；只出现 2 页的行不满足 >= 3 页
    assert build_repeated_lines(pages) == set()


def test_repeated_lines_same_page_duplicate_not_counted():
    # 同一页内重复多次的行只算 1 页
    pages = {
        1: {"page_text": "Header Line\nHeader Line\nHeader Line"},
        2: {"page_text": "Header Line"},
        3: {"page_text": "Header Line"},
    }
    assert "Header Line" in build_repeated_lines(pages)


# ---- Fix 1: coverage invariant ----

def _covered_pages(sections):
    covered = set()
    for s in sections:
        for p in range(s[1], s[2] + 1):
            covered.add(p)
    return covered


def test_coverage_invariant_fills_gaps():
    # 区间 1-10 和 20-30，缺 11-19：gap 必须被并入相邻章节
    sections = [("a", 1, 10, 1, "A", "a"), ("b", 20, 30, 1, "B", "b")]
    fixed = ensure_page_coverage(sections, 30)
    assert coverage_invariant_holds(fixed, 30)
    assert _covered_pages(fixed) == set(range(1, 31))
    # 区间数量不变（gap 并入已有章节，不新建）
    assert len(fixed) == 2


def test_coverage_invariant_no_sections_creates_fallback():
    fixed = ensure_page_coverage([], 50)
    assert len(fixed) == 1
    assert fixed[0][0] == "_body"
    assert fixed[0][1] == 1
    assert fixed[0][2] == 50
    assert coverage_invariant_holds(fixed, 50)


def test_coverage_invariant_already_covered_unchanged():
    sections = [("a", 1, 10, 1, "A", "a"), ("b", 11, 20, 1, "B", "b")]
    fixed = ensure_page_coverage(sections, 20)
    assert fixed == sections
    assert coverage_invariant_holds(fixed, 20)


def test_coverage_invariant_gap_before_first_section():
    # 页 1-4 无章节，章节从 5 开始：gap 并入最近后置章节
    sections = [("a", 5, 10, 1, "A", "a")]
    fixed = ensure_page_coverage(sections, 10)
    assert coverage_invariant_holds(fixed, 10)
    assert fixed[0][1] == 1  # start 前移到 1


# ---- Fix 4: composite identity + consumer ----

def test_structure_index_multi_segment_same_path(tmp_path):
    from core.db.tenant_db import TenantMetadataDB

    db = TenantMetadataDB(tmp_path / "test_meta.db")
    db.save_structure_index("doc1", "1.2", "Section A", 2, 10, 12)
    db.save_structure_index("doc1", "1.2", "Section A", 2, 20, 22)
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT section_path, start_page, end_page FROM doc_structure_index "
            "WHERE doc_id='doc1' ORDER BY start_page"
        ).fetchall()
    assert len(rows) == 2  # 同 path 两段俱在（复合键生效）
    assert tuple(rows[0][0:3]) == ("1.2", 10, 12)
    assert tuple(rows[1][0:3]) == ("1.2", 20, 22)


def test_expand_section_pages_sees_both_segments(tmp_path):
    """复合键让同 path 的每一段都可被展开（旧代码 path 单键 dict 只留
    最后一段，第一段永远不可达）。"""
    from core.db.tenant_db import TenantMetadataDB
    from core.retrieval.retriever import SegmentMerger

    db = TenantMetadataDB(tmp_path / "test_meta.db")
    db.save_structure_index("doc1", "1.2", "Section A", 2, 10, 12)
    db.save_structure_index("doc1", "1.2", "Section A", 2, 20, 22)
    with db.get_connection() as conn:
        for pn in (10, 11, 12, 20, 21, 22):
            conn.execute(
                "INSERT OR REPLACE INTO doc_pages (doc_id, page_num, raw_text) VALUES (?, ?, ?)",
                ("doc1", pn, f"page {pn} content"),
            )
        conn.commit()

    merger = SegmentMerger(tenant_id=None)
    # 命中第一段：展开第一段整段
    expanded1 = merger._expand_section_pages("doc1", {11}, db)
    assert set(expanded1.keys()) == {10, 12}
    # 命中第二段：展开第二段整段（旧代码只留最后一段时第一段永远不可达）
    expanded2 = merger._expand_section_pages("doc1", {21}, db)
    assert set(expanded2.keys()) == {20, 22}
    assert "page 20 content" in expanded2[20]["raw_text"]
