"""Embedding failure visibility (issue: silent hollow ingestion).

A document whose chunks fail embedding must be loudly visible: deterministic
HTTP 4xx rejections fail fast without pointless retries, losses are bucketed
by cause, and the per-document summary escalates to ERROR whenever any chunk
is lost — previously "successfully stored 32" was logged at INFO while 272
chunks had been silently skipped.
"""
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest
import requests

# builder.py top-level imports PIL/numpy (via layout), which the CI venv
# intentionally lacks. Stub them before importing, as test_ingestion_logic
# does for fitz — only annotations/attributes are touched at import time.
# When PIL/numpy ARE installed (dev machines), import the real modules so
# the stubs don't shadow them for tests collected later in the run.
try:
    import PIL  # noqa: F401
    import PIL.Image  # noqa: F401
except ImportError:
    _pil_image = types.ModuleType("PIL.Image")
    _pil_image.Image = type("Image", (), {})
    _pil = types.ModuleType("PIL")
    _pil.Image = _pil_image
    for _name, _mod in (("PIL", _pil), ("PIL.Image", _pil_image)):
        sys.modules.setdefault(_name, _mod)
try:
    import numpy  # noqa: F401
except ImportError:
    _np = types.ModuleType("numpy")
    _np.ndarray = type("ndarray", (), {})
    sys.modules.setdefault("numpy", _np)

from core.ingestion.builder import DocumentIndexBuilder  # noqa: E402
from core.models.client import (  # noqa: E402
    EmbeddingError,
    ModelClient,
    _classify_embedding_error,
)

# --------------------------------------------------------------------------
# client-layer classification
# --------------------------------------------------------------------------

def _make_client():
    c = object.__new__(ModelClient)  # bypass network-touching __init__
    c._session = MagicMock()
    c.embedding_base_url = "http://emb.test"
    c.embedding_model = "m"
    c.embedding_api_key = ""
    return c


def _http_error(status):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


def test_classify_http_4xx_is_rejected():
    _, kind = _classify_embedding_error(_http_error(500).raise_for_status.side_effect)
    assert kind == "other"
    err = _http_error(400).raise_for_status.side_effect
    status, kind = _classify_embedding_error(err)
    assert (status, kind) == (400, "rejected")


def test_classify_timeout():
    _, kind = _classify_embedding_error(requests.exceptions.Timeout("t"))
    assert kind == "timeout"


def test_classify_llama_500_too_large_is_rejected():
    """llama-server reports physical-batch overflow as HTTP 500 with a stable
    'too large to process' message — deterministic, must not be retried."""
    resp = MagicMock()
    resp.status_code = 500
    resp.text = '{"error": "input (1066 tokens) is too large to process. increase the physical batch size"}'
    err = requests.exceptions.HTTPError(response=resp)
    status, kind = _classify_embedding_error(err)
    assert (status, kind) == (500, "rejected")
    # A plain 500 without that signature stays retryable
    resp2 = MagicMock()
    resp2.status_code = 500
    resp2.text = "internal error"
    _, kind2 = _classify_embedding_error(requests.exceptions.HTTPError(response=resp2))
    assert kind2 == "other"


def test_embed_batch_wraps_http_500_as_other():
    c = _make_client()
    c.session.post.return_value = _http_error(500)
    with pytest.raises(EmbeddingError) as ei:
        c.embed_batch(["hello"])
    assert ei.value.status_code == 500
    assert ei.value.kind == "other"


def test_embed_batch_wraps_http_400_as_rejected():
    c = _make_client()
    c.session.post.return_value = _http_error(400)
    with pytest.raises(EmbeddingError) as ei:
        c.embed_batch(["hello"])
    assert ei.value.status_code == 400
    assert ei.value.kind == "rejected"


def test_embed_batch_wraps_timeout():
    c = _make_client()
    c.session.post.side_effect = requests.exceptions.Timeout("slow")
    with pytest.raises(EmbeddingError) as ei:
        c.embed_batch(["hello"])
    assert ei.value.kind == "timeout"


# --------------------------------------------------------------------------
# builder-layer behaviour
# --------------------------------------------------------------------------

def _make_builder(n_pages: int, embed_side_effect):
    """DocumentIndexBuilder with DBs/model client mocked; distinct sections
    prevent adjacent-page merging, so each page yields exactly 1 chunk."""
    b = object.__new__(DocumentIndexBuilder)  # bypass heavy __init__
    pages = [
        {"id": f"page{i}", "page_num": i, "raw_text": f"第{i}页内容 " * 60,
         "section_path": f"sec{i}", "section_title": f"Section {i}"}
        for i in range(1, n_pages + 1)
    ]
    metadata_db = MagicMock()
    metadata_db.get_document_pages.return_value = pages
    vector_db = MagicMock()
    b._get_dbs = lambda tenant_id=None: (metadata_db, vector_db)
    mc = MagicMock()
    mc.embed_batch.side_effect = embed_side_effect
    b.model_client = mc
    return b, metadata_db, vector_db


def _ok_embeddings(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


def test_rejected_batch_fails_fast_without_retry(monkeypatch, caplog):
    monkeypatch.setattr("time.sleep", lambda s: None)
    b, _, _ = _make_builder(3, EmbeddingError("400 too large", status_code=400, kind="rejected"))
    with caplog.at_level(logging.WARNING):
        b._build_embeddings("doc1", [], "t1")
    assert b.model_client.embed_batch.call_count == 1  # no pointless retries
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("rejected, not retrying" in r.getMessage() for r in errors)
    summary = [r for r in errors if "LOST" in r.getMessage()]
    assert summary, "lost-chunk summary must be logged at ERROR"
    assert "rejected=3" in summary[0].getMessage()


def test_timeout_batch_retries_three_times(monkeypatch, caplog):
    monkeypatch.setattr("time.sleep", lambda s: None)
    b, _, _ = _make_builder(3, EmbeddingError("timeout", kind="timeout"))
    with caplog.at_level(logging.WARNING):
        b._build_embeddings("doc1", [], "t1")
    assert b.model_client.embed_batch.call_count == 3  # transient: retry 1+2
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("final failure" in r.getMessage() for r in errors)
    summary = [r for r in errors if "LOST" in r.getMessage()]
    assert summary and "timeout=3" in summary[0].getMessage()


def test_full_success_logs_info_not_error(caplog):
    b, metadata_db, vector_db = _make_builder(3, _ok_embeddings)
    with caplog.at_level(logging.INFO):
        b._build_embeddings("doc1", [], "t1")
    assert vector_db.store_l2_chunk.call_count == 3
    assert metadata_db.save_chunk.call_count == 3
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("successfully stored 3" in r.getMessage() for r in infos)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_partial_failure_summary_buckets_losses(monkeypatch, caplog):
    """batch_size=8 → 10 pages make 2 batches (8+2); 1st OK, 2nd rejected."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok_embeddings(texts)
        raise EmbeddingError("400 too large", status_code=400, kind="rejected")

    b, metadata_db, vector_db = _make_builder(10, flaky)
    with caplog.at_level(logging.WARNING):
        warnings = b._build_embeddings("doc1", [], "t1")
    stored = vector_db.store_l2_chunk.call_count
    assert stored == 8  # only the first batch made it
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    summary = [r for r in errors if "LOST" in r.getMessage()]
    assert summary, "partial loss must escalate to ERROR summary"
    msg = summary[0].getMessage()
    assert "stored 8" in msg and "LOST 2" in msg and "rejected=2" in msg
    # returned warnings feed document-level ingest_warnings / degraded status
    assert warnings == ["2/10 chunks not embedded (rejected=2)"]


def test_full_success_returns_no_warnings(caplog):
    b, _, _ = _make_builder(3, _ok_embeddings)
    with caplog.at_level(logging.INFO):
        warnings = b._build_embeddings("doc1", [], "t1")
    assert warnings == []
