"""FTS dominance red line: the vector channel must never remove an FTS result.

Project rule (stated twice in the source, at ``config.py`` and ``retriever.py``):

    "This block must NOT replace or delete any FTS result."

Until now that promise lived only in comments — ``grep hybrid_vector tests/``
returned nothing. These tests make it executable:

  * the set of documents reached by FTS cannot shrink when the vector channel
    runs (gap-fill only appends, confirm-boost only re-scores);
  * a gap-filled page must not outrank an FTS *leader* (it is inserted with a
    deliberately low base score);
  * disabling the channel must leave ranking untouched (the off switch is a
    true rollback path).

The scoring arithmetic is reproduced from ``retriever._retrieve_exact`` so the
constants under test are the production ones, not test-local copies.
"""

from core.config import settings

HV = settings.CONTEXT_CONFIG


class Page:
    def __init__(self, doc_id, page_id, page_num, text="x", title=""):
        self.doc_id = doc_id
        self.page_id = page_id
        self.page_num = page_num
        self.raw_text = text
        self.section_title = title


def apply_vector_channel(fts_rows, vector_hits, cfg=HV):
    """Reproduce the A (confirm-boost) and B (gap-fill) blocks verbatim.

    ``fts_rows``: list of dicts {doc_id, page_id, score}
    ``vector_hits``: list of dicts {doc_id, page_id, score}
    Returns new row list in the same shape.
    """
    rows = [dict(r) for r in fts_rows]
    if not cfg.get("hybrid_vector_enabled", True) or not rows:
        return rows

    hv_per_doc = cfg.get("hybrid_vector_per_doc", 4)
    hv_scale = cfg.get("hybrid_vector_boost_scale", 25.0)
    hv_base = cfg.get("hybrid_vector_supplement_base", 5.0)

    vec_by_page = {}
    for h in vector_hits:
        key = (h["doc_id"], h["page_id"])
        if key not in vec_by_page or h["score"] > vec_by_page[key]:
            vec_by_page[key] = h["score"]

    # A. confirm-boost
    for r in rows:
        vs = vec_by_page.get((r["doc_id"], r["page_id"]))
        if vs is not None:
            r["score"] += vs * hv_scale

    # B. gap-fill
    existing = {(r["doc_id"], r["page_id"]) for r in rows}
    per_doc = {}
    for h in vector_hits:
        key = (h["doc_id"], h["page_id"])
        if key in existing:
            continue
        if per_doc.get(h["doc_id"], 0) >= hv_per_doc:
            continue
        rows.append({
            "doc_id": h["doc_id"], "page_id": h["page_id"],
            "score": hv_base + h["score"] * hv_scale * 0.1,
            "vector_supplement": True,
        })
        existing.add(key)
        per_doc[h["doc_id"]] = per_doc.get(h["doc_id"], 0) + 1
    return rows


def keys(rows):
    return {(r["doc_id"], r["page_id"]) for r in rows}


FTS_ROWS = [
    {"doc_id": "d1", "page_id": "p1", "score": 100.0},
    {"doc_id": "d1", "page_id": "p2", "score": 50.0},
    {"doc_id": "d1", "page_id": "p3", "score": 10.0},
]

VECTOR_HITS = [
    # confirms an FTS page (p3) strongly
    {"doc_id": "d1", "page_id": "p3", "score": 0.95},
    # a page FTS never returned
    {"doc_id": "d1", "page_id": "p9", "score": 0.80},
]


class TestFtsResultsSurviveTheVectorChannel:
    def test_no_fts_page_is_dropped(self):
        before = keys(FTS_ROWS)
        after = keys(apply_vector_channel(FTS_ROWS, VECTOR_HITS))
        missing = before - after
        assert not missing, (
            f"vector channel deleted FTS results {missing}; the red line is "
            "'must NOT replace or delete any FTS result'"
        )

    def test_gap_fill_only_appends(self):
        before = keys(FTS_ROWS)
        after = keys(apply_vector_channel(FTS_ROWS, VECTOR_HITS))
        assert before <= after, "gap-fill must be additive only"
        assert len(after) > len(before), "test fixture should exercise a gap fill"

    def test_confirmed_page_is_boosted_not_replaced(self):
        out = {r["page_id"]: r for r in apply_vector_channel(FTS_ROWS, VECTOR_HITS)}
        assert out["p3"]["score"] > 10.0, "confirmed page should gain score"

    def test_disabled_channel_leaves_ranking_identical(self):
        cfg_off = dict(HV)
        cfg_off["hybrid_vector_enabled"] = False
        off = apply_vector_channel(FTS_ROWS, VECTOR_HITS, cfg=cfg_off)
        assert off == [dict(r) for r in FTS_ROWS], (
            "with the switch off the vector channel must be a no-op (rollback path)"
        )


class TestGapFillDoesNotOutrankFtsLeaders:
    def test_gap_filled_page_scores_below_fts_leader(self):
        out = apply_vector_channel(FTS_ROWS, VECTOR_HITS)
        leader = max(r["score"] for r in out if not r.get("vector_supplement"))
        best_fill = max((r["score"] for r in out if r.get("vector_supplement")),
                        default=0.0)
        assert best_fill < leader, (
            f"gap-filled page scored {best_fill:.2f}, FTS leader scored "
            f"{leader:.2f}; a page FTS never returned must not lead the result"
        )

    def test_per_doc_cap_is_respected(self):
        many = [{"doc_id": "d1", "page_id": f"z{i}", "score": 0.9} for i in range(20)]
        out = apply_vector_channel(FTS_ROWS, many)
        filled = [r for r in out if r.get("vector_supplement")]
        assert len(filled) <= HV.get("hybrid_vector_per_doc", 4), (
            "gap-fill must stay capped per document"
        )


class TestBoostScaleStaysBounded:
    """A guard on the *constants*: a runaway scale is the realistic way to
    break the red line without deleting anything (it can push a low FTS page
    past a high one)."""

    def test_boost_scale_cannot_lift_a_low_fts_page_past_a_strong_one(self):
        # p3 (FTS 10) confirmed at the max possible similarity (1.0)
        out = {r["page_id"]: r for r in apply_vector_channel(
            FTS_ROWS, [{"doc_id": "d1", "page_id": "p3", "score": 1.0}])}
        assert out["p3"]["score"] < out["p1"]["score"], (
            f"a confirmed-but-weak FTS page (score {out['p3']['score']:.1f}) now "
            f"outranks a strong unconfirmed one ({out['p1']['score']:.1f}); "
            "the boost scale is large enough to override FTS ordering"
        )

    def test_min_score_threshold_is_meaningful(self):
        assert 0.0 < HV.get("hybrid_vector_min_score", 0.45) < 1.0, (
            "vector threshold must be a similarity in (0,1)"
        )

    def test_supplement_base_is_low(self):
        assert HV.get("hybrid_vector_supplement_base", 5.0) <= 10.0, (
            "gap-fill base score is documented as low; a large value would let "
            "vector-only pages displace FTS results"
        )
