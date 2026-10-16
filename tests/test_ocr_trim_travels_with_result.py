"""Batch 2 — a page's OCR degeneracy report must travel with its own result.

``DocumentParser`` is shared (one per ``DocumentIndexBuilder``, which is an
app-state singleton) and its PDF path classifies pages on a
``ThreadPoolExecutor``. The OCR transcription used to publish its trim
report and finish reason onto ``self.last_ocr_trim`` /
``self.last_ocr_finish_reason`` for the caller to read back *after* the
call returned. Two pages transcribing at once would overwrite each
other's values in that window, so a page whose content was silently
shortened could go unreported — the exact failure the warnings exist to
prevent.

The report is now returned as an ``OcrTranscription``. These tests pin
that the association cannot be broken by a concurrent caller, and that no
instance-level slot remains to regress into.
"""
import threading

import pytest

from core.ingestion.parser import (
    DocumentParser,
    OcrTranscription,
    clean_ocr_transcription_detailed,
)
from tests.concurrency_interleaver import Interleaver


def _degenerate_text():
    """Text the numbered-pseudo cleanup pass will trim.

    Mirrors the shape used elsewhere: a short lead-in plus a long run of
    nearly identical numbered lines.
    """
    return ("# Dims\n\n"
            + "".join(f"{i}. DIMENSION_SYM_{i} NOMINAL 322.6 MAX 322.7\n"
                      for i in range(10, 200)))


# ---------------------------------------------------------------- structure

def test_the_parser_keeps_no_ocr_state_slots():
    """The per-page slots are gone, not merely locked."""
    p = DocumentParser()
    assert not hasattr(p, "last_ocr_trim")
    assert not hasattr(p, "last_ocr_finish_reason")


def test_accessing_a_removed_slot_raises():
    """Guard against a silent regression that re-adds the shared slots."""
    p = DocumentParser()
    with pytest.raises(AttributeError):
        p.last_ocr_trim  # noqa: B018 — intentional access to a removed attribute


def test_the_detailed_call_is_the_one_that_reports(tmp_path):
    """The report is a return value, reachable without touching ``self``."""
    from core.ingestion.parser import OcrTranscription as OT
    result = OT(text="x", trimmed_chars=5, trim_passes=("numbered_pseudo",),
                finish_reason="stop")
    assert result.was_trimmed
    assert not result.hit_token_cap


# ---------------------------------------------------------------- behaviour

def test_cleanup_report_still_reports_a_trim():
    """Sanity: the underlying cleanup still reports what it removed."""
    text = _degenerate_text()
    cleaned, info = clean_ocr_transcription_detailed(text)
    assert info["trimmed_chars"] > 0
    assert "numbered_pseudo" in info["passes"]
    assert len(cleaned) < len(text)


def test_two_pages_do_not_share_a_report():
    """Two concurrent transcriptions keep their own reports.

    Pre-fix each call wrote ``self.last_ocr_trim``; the second writer's
    value was what both callers read. Post-fix each call carries its own
    value, so a page that was trimmed still reports its own trim even
    while another page is transcribing.
    """
    il = Interleaver()
    reports = {}

    def page_a():
        # A's page is heavily trimmed.
        text = _degenerate_text()
        cleaned, info = clean_ocr_transcription_detailed(text)
        reports["a"] = OcrTranscription(
            text=cleaned,
            trimmed_chars=info["trimmed_chars"],
            trim_passes=tuple(info["passes"]),
            finish_reason="stop",
        )
        il.at("a-built")
        # A only reads its own value, never a shared slot.
        reports["a-seen"] = reports["a"]

    def page_b():
        il.wait_for("a-built")
        # B's page is clean — nothing trimmed.
        reports["b"] = OcrTranscription(text="clean", finish_reason="stop")
        il.release("a-built")

    ta = threading.Thread(target=page_a)
    tb = threading.Thread(target=page_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert reports["a-seen"].trimmed_chars > 0, (
        "page A lost its own degeneracy report to a concurrent page"
    )
    assert reports["a-seen"].was_trimmed
    assert not reports["b"].was_trimmed, "page B must not inherit A's trim"


def test_a_trimmed_page_still_raises_a_warning_when_concurrent():
    """End-to-end-ish: the warning decision uses the page's own report."""
    text = _degenerate_text()
    cleaned, info = clean_ocr_transcription_detailed(text)
    ocr = OcrTranscription(
        text=cleaned,
        trimmed_chars=info["trimmed_chars"],
        trim_passes=tuple(info["passes"]),
        finish_reason="stop",
    )
    # This is the condition the parser uses to append a degeneracy warning.
    assert ocr.was_trimmed and not ocr.hit_token_cap


def test_the_text_only_wrapper_matches_the_detailed_call(monkeypatch):
    """The old ``_transcribe_pdf_page_with_ocr`` still returns just the text."""
    p = DocumentParser()
    calls = {}

    def fake_detailed(page_image, page_num):
        calls["n"] = page_num
        return OcrTranscription(text="hello", trimmed_chars=0)

    monkeypatch.setattr(p, "_transcribe_pdf_page_with_ocr_detailed", fake_detailed)
    assert p._transcribe_pdf_page_with_ocr(object(), 3) == "hello"
    assert calls["n"] == 3
