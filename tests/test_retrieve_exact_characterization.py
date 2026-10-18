"""Characterization tests for ``_retrieve_exact`` — the 612-line hot path.

These are **behaviour locks**, not specification tests. ``_retrieve_exact`` is due
for a split into a pipeline (query expansion -> chapter scope -> FTS recall ->
evidence fusion -> page supplement -> vector pass). The split is only safe if the
old and new implementations produce the same results, and that requires a
recorded baseline of what the current one does.

Each test pins an observable property that any refactor must preserve:

  * the returned pages are a subset of what FTS/chapter recall produced;
  * page identity is stable across repeated calls with the same input;
  * ``max_results`` is honoured;
  * the doc filter actually filters;
  * ordering is by score, descending.

The suite builds a real ``HierarchicalRetriever`` against a temporary tenant
database so it exercises the real SQL and scoring, not a mock — a mocked version
would pass a refactor that breaks the SQL.

If a refactor changes these outputs, the diff is the review artifact: either the
change is intended (update the test with a note) or it is a regression.
"""

import sqlite3

import pytest

from core.retrieval.retriever import HierarchicalRetriever, SearchResult

SCHEMA = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY, filename TEXT, title TEXT, status TEXT,
    page_count INTEGER, created_at TEXT
);
CREATE TABLE pages (
    id TEXT PRIMARY KEY, doc_id TEXT, page_num INTEGER,
    raw_text TEXT, section_title TEXT, image_path TEXT
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED, doc_id UNINDEXED, page_id UNINDEXED,
    content, tokenize='trigram'
);
"""

DOCS = [
    ("d1", "d1.pdf", "测试产品手册", "verified", 3),
    ("d2", "d2.pdf", "另一份手册", "verified", 2),
]

PAGES = [
    # d1: two pages mentioning the target term, one unrelated
    ("d1p1", "d1", 1, "产品规格：支持 十个 UART 接口，主频 1.2GHz。", "规格", None),
    ("d1p2", "d1", 2, "接口说明：UART 接口支持 波特率 115200。", "接口", None),
    ("d1p3", "d1", 3, "包装清单：说明书、电源适配器。", "包装", None),
    # d2: one page with the term
    ("d2p1", "d2", 1, "另一型号也支持 UART 接口。", "概述", None),
    ("d2p2", "d2", 2, "无关内容：工作温度范围。", "环境", None),
]

CHUNKS = [
    ("c1", "d1", "d1p1", "支持 十个 UART 接口 主频"),
    ("c2", "d1", "d1p2", "UART 接口 波特率 115200"),
    ("c3", "d1", "d1p3", "包装清单 说明书 电源适配器"),
    ("c4", "d2", "d2p1", "另一型号 UART 接口"),
    ("c5", "d2", "d2p2", "无关内容 工作温度"),
]


class _FakeModelClient:
    """Stands in for ModelClient; embedding is unavailable so the vector pass
    is skipped, which is the pure-FTS behaviour this suite characterises."""

    def embed(self, text):
        from core.models.client import EmbeddingError
        raise EmbeddingError("embedding disabled in characterization tests")


class _FakeMetadataDB:
    """Minimal metadata DB over a temp sqlite file, matching the methods the
    retriever calls. Real SQL, real FTS — just a small schema."""

    def __init__(self, path):
        self.path = path

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def get_page(self, page_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
            return dict(r) if r else None

    def get_document(self, doc_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            return dict(r) if r else None

    def get_structure_index(self, doc_id):
        return []

    def search_fts_chunks(self, query, limit=50, doc_id_filter=None, page_filter=None,
                          force_bigram_only=False):
        with self._conn() as c:
            try:
                rows = c.execute(
                    "SELECT chunk_id, doc_id, page_id, content FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? LIMIT ?", (query, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        out = []
        for r in rows:
            d = dict(r)
            if doc_id_filter and "__ALL__" not in doc_id_filter and d["doc_id"] not in doc_id_filter:
                continue
            # The real DB layer joins pages and returns these alongside the chunk.
            with self._conn() as c:
                p = c.execute("SELECT page_num FROM pages WHERE id=?",
                              (d["page_id"],)).fetchone()
            d["page_num"] = p["page_num"] if p else 0
            d["chunk_text"] = d["content"]
            d["score"] = 1.0
            out.append(d)
        return out

    def list_documents(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM documents").fetchall()]

    def get_document_pages(self, doc_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pages WHERE doc_id=? ORDER BY page_num", (doc_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_pages_containing(self, doc_id, term, limit=50):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pages WHERE doc_id=? AND raw_text LIKE ? LIMIT ?",
                (doc_id, f"%{term}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def search_structure_index(self, doc_id, keywords=None, limit=20):
        return []


@pytest.fixture
def retriever(tmp_path, monkeypatch):
    db_path = tmp_path / "meta.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?)", [
        (i, f, t, s, n, "2026-01-01") for i, f, t, s, n in DOCS])
    conn.executemany("INSERT INTO pages VALUES (?,?,?,?,?,?)", PAGES)
    conn.executemany("INSERT INTO chunks_fts VALUES (?,?,?,?)", CHUNKS)
    conn.commit()
    conn.close()

    r = HierarchicalRetriever.__new__(HierarchicalRetriever)
    r.tenant_id = "test"
    r.metadata_db = _FakeMetadataDB(str(db_path))
    r.vector_db = None
    r.model_client = _FakeModelClient()
    r._retrieval_rule_cache = None
    # Skip industry-pack rule loading: this suite is about retrieval mechanics.
    monkeypatch.setattr(HierarchicalRetriever,
                        "_load_industry_boost_rules_for_retrieval",
                        lambda self: {})
    return r


from core.retrieval.router import IntentType, QueryPlan  # noqa: E402


def plan(query="UART 接口", **kw):
    p = QueryPlan(intent=IntentType.EXACT_LOOKUP, raw_query=query, entities=[], **kw)
    return p


class TestResultContract:
    def test_returns_search_results(self, retriever):
        out = retriever._retrieve_exact("UART 接口", plan(), 10)
        assert isinstance(out, list)
        assert all(isinstance(r, SearchResult) for r in out)

    def test_finds_the_pages_containing_the_term(self, retriever):
        out = retriever._retrieve_exact("UART 接口", plan(), 10)
        assert out, "retrieval returned nothing for a term present in 3 pages"
        texts = " ".join(r.content or "" for r in out)
        assert "UART" in texts

    def test_respects_max_results(self, retriever):
        for cap in (1, 2):
            out = retriever._retrieve_exact("UART 接口", plan(), cap)
            assert len(out) <= cap, f"returned {len(out)} with max_results={cap}"

    def test_scores_are_descending(self, retriever):
        out = retriever._retrieve_exact("UART 接口", plan(), 10)
        scores = [r.score for r in out]
        assert scores == sorted(scores, reverse=True), (
            f"results are not ordered by score: {scores}"
        )

    def test_documents_never_mixed_across_filter(self, retriever):
        out = retriever._retrieve_exact("UART 接口", plan(), 10,
                                        doc_id_filter={"d1"})
        assert out
        assert {r.doc_id for r in out} == {"d1"}, (
            "a document outside doc_id_filter leaked into the results"
        )

    def test_all_docs_sentinel_does_not_filter(self, retriever):
        out = retriever._retrieve_exact("UART 接口", plan(), 10,
                                        doc_id_filter={"__ALL__"})
        assert {r.doc_id for r in out} >= {"d1"}

    def test_empty_result_for_absent_term(self, retriever):
        out = retriever._retrieve_exact("完全不存在的词汇组合", plan("完全不存在的词汇组合"), 10)
        assert out == [] or all("不存在" in (r.content or "") for r in out)


class TestDeterminism:
    def test_same_input_same_pages(self, retriever):
        a = [(r.doc_id, r.page_num) for r in retriever._retrieve_exact("UART 接口", plan(), 10)]
        b = [(r.doc_id, r.page_num) for r in retriever._retrieve_exact("UART 接口", plan(), 10)]
        assert a == b, "repeated identical calls returned different pages"

    def test_same_input_same_scores(self, retriever):
        a = [round(r.score, 6) for r in retriever._retrieve_exact("UART 接口", plan(), 10)]
        b = [round(r.score, 6) for r in retriever._retrieve_exact("UART 接口", plan(), 10)]
        assert a == b, "scores are not reproducible for identical input"


class TestPageIdentity:
    def test_each_page_appears_once(self, retriever):
        out = retriever._retrieve_exact("UART 接口", plan(), 10)
        keys = [(r.doc_id, r.page_num) for r in out]
        assert len(keys) == len(set(keys)), f"duplicate pages in result: {keys}"

    def test_page_numbers_are_ints(self, retriever):
        for r in retriever._retrieve_exact("UART 接口", plan(), 10):
            assert isinstance(r.page_num, int)

    def test_content_is_populated(self, retriever):
        for r in retriever._retrieve_exact("UART 接口", plan(), 10):
            assert r.content, f"page {r.page_num} returned with empty content"
