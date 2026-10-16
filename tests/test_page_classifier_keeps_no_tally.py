"""Batch 1 — the page classifier must not carry a shared tally.

The classifier used to be a process-wide singleton holding ``_stats``,
mutated by every page thread and read+reset by whichever document
finished first (``parser._parse_pdf``). Under the parser's page-level
``ThreadPoolExecutor`` the counter mixed documents together, and the
first document to finish wiped the others' counts before they read them.

The fix removes the tally entirely: ``classify()`` returns the label and
the caller aggregates the labels it collected. These tests pin both
halves — the behaviour (labels return correctly, unaffected by concurrent
callers) and the structure (no tally to regress into).
"""
import threading

import pytest

from core.ingestion.layout.page_classifier import PageClassifier, is_blank_image
from tests.concurrency_interleaver import Interleaver


class _FakeImage:
    """Minimal stand-in for a PIL image.

    ``is_blank_image`` calls ``.convert("L")`` then ``.histogram()``. A
    grayscale histogram is 256 buckets; putting mass above the white
    threshold marks the page blank, mass below it marks it non-blank.
    """

    def __init__(self, blank: bool):
        self._blank = blank
        self.mode = "RGB"

    def convert(self, _mode):
        return self

    def histogram(self):
        hist = [0] * 256
        if self._blank:
            hist[255] = 10000  # everything white
        else:
            hist[10] = 10000   # everything dark
        return hist

    @property
    def width(self):
        return 100

    @property
    def height(self):
        return 100

    def save(self, *a, **k):
        pass


# ---------------------------------------------------------------- structure

def test_the_classifier_keeps_no_tally():
    """The shared counter is gone, not merely locked."""
    pc = PageClassifier()
    assert not hasattr(pc, "_stats"), (
        "PageClassifier must not carry a per-call tally: it is shared across "
        "tenants and page threads, so the numbers belong to no single document"
    )
    assert not hasattr(pc, "reset_stats")


def test_the_classifier_is_no_longer_a_singleton():
    """Two instances must be independent — the singleton was the sharing."""
    assert PageClassifier() is not PageClassifier(), (
        "PageClassifier is still a singleton; a shared instance is exactly "
        "how one document's page state leaks into another's"
    )


def test_reading_a_removed_stat_raises_rather_than_returning_a_stale_dict():
    """Guard against a silent regression that re-adds the tally."""
    pc = PageClassifier()
    with pytest.raises(AttributeError):
        pc.stats  # noqa: B018 — intentional access to a removed attribute


# ---------------------------------------------------------------- behaviour

def test_classify_still_reports_blank_pages():
    assert PageClassifier().classify(_FakeImage(blank=True), 1) == "BLANK"


def test_classify_does_not_accumulate_across_calls():
    """Repeated calls must not build up any observable counter."""
    pc = PageClassifier()
    for i in range(5):
        pc.classify(_FakeImage(blank=True), i)
    # If a tally were reintroduced, an aggregate like `stats` would appear.
    assert not hasattr(pc, "stats")
    assert not hasattr(pc, "_stats")


def test_concurrent_classifiers_do_not_share_state():
    """Two documents classifying at once must not see each other's pages.

    Pre-fix, whichever document finished first reset the shared dict,
    discarding the other's counts. Post-fix there is nothing to share, so
    the only assertion that can fail is a reintroduced shared slot.
    """
    il = Interleaver()
    seen = {}

    def document_a(pc):
        pc.classify(_FakeImage(blank=True), 1)
        il.at("a-classified")
        seen["a"] = pc.classify(_FakeImage(blank=True), 2)

    def document_b(pc):
        il.wait_for("a-classified")
        pc.classify(_FakeImage(blank=True), 1)  # B's page, same instance
        il.release("a-classified")

    # Same instance on purpose: this is the shape that used to cross-talk.
    shared = PageClassifier()
    ta = threading.Thread(target=document_a, args=(shared,))
    tb = threading.Thread(target=document_b, args=(shared,))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert seen["a"] == "BLANK"
    assert not hasattr(shared, "_stats"), (
        "a shared PageClassifier must not hold mutable per-call state"
    )


def test_a_real_blank_image_is_still_detected():
    """Sanity: ``is_blank_image`` itself is untouched by this change."""
    assert is_blank_image(_FakeImage(blank=True)) is True
    assert is_blank_image(_FakeImage(blank=False)) is False
