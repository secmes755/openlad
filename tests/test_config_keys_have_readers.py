"""Every configuration key must have a reader.

The codebase had 31 keys in its CONFIG dicts that nothing read — including a
dozen ``*_chars`` budgets whose names implied they controlled context size. A
dead knob is worse than no knob: it passes review as a live setting, invites
tuning that changes nothing, and makes the real budget hard to find.

This is a meta-test. It scans the repository for references to each key and
fails on any that nothing uses, so the debt cannot silently re-accumulate. When
a key is genuinely needed only by an operator (or is about to be wired up), add
it to ``INTENTIONALLY_UNREAD`` with a reason — the test then documents the
exception instead of hiding it.
"""

import os
import re

import pytest

from core.config import settings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keys with no code reader that are deliberately kept.
# Format: {key: reason}
INTENTIONALLY_UNREAD: dict[str, str] = {}

# Directories that constitute "the codebase" for this check.
SCAN_DIRS = ["core", "tests", "scripts"]
SCAN_EXTS = (".py", ".js", ".sh", ".yaml", ".yml")


def _all_config_keys() -> dict[str, str]:
    """(key -> owning CONFIG block) for every key in every *_CONFIG dict."""
    out = {}
    for attr in dir(settings):
        if not attr.endswith("_CONFIG"):
            continue
        block = getattr(settings, attr)
        if isinstance(block, dict):
            for k in block:
                out[k] = attr
    return out


def _source_files():
    for d in SCAN_DIRS:
        root_dir = os.path.join(REPO, d)
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "node_modules", "dist")]
            for f in files:
                if f.endswith(SCAN_EXTS):
                    yield os.path.join(root, f)


def _readers(key: str, files: list[str]) -> list[str]:
    """Files that reference the key as a string literal (any access style)."""
    pat = re.compile(rf"""['"]{re.escape(key)}['"]""")
    me = os.path.abspath(__file__)
    hits = []
    for p in files:
        if os.path.abspath(p) == me:
            continue  # this test names keys in its own assertions
        try:
            src = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if pat.search(src):
            # config.py defines the keys, so it is not a reader.
            if os.path.basename(p) == "config.py" and os.path.dirname(p).endswith("core"):
                continue
            hits.append(p)
    return hits


@pytest.fixture(scope="module")
def files():
    return list(_source_files())


def test_no_config_key_is_unread(files):
    keys = _all_config_keys()
    assert keys, "no CONFIG dicts found — the settings surface moved?"

    dead = []
    for key, block in sorted(keys.items()):
        if key in INTENTIONALLY_UNREAD:
            continue
        if not _readers(key, files):
            dead.append(f"{block}.{key}")

    assert not dead, (
        "configuration keys with no reader anywhere in the codebase:\n  "
        + "\n  ".join(dead)
        + "\n\nEither wire them up, delete them, or list them in "
        "INTENTIONALLY_UNREAD with a reason."
    )


def test_intentionally_unread_entries_are_real(files):
    """An exemption for a key that no longer exists is stale bookkeeping."""
    keys = _all_config_keys()
    stale = [k for k in INTENTIONALLY_UNREAD if k not in keys]
    assert not stale, f"INTENTIONALLY_UNREAD lists keys that do not exist: {stale}"


def test_intentionally_unread_entries_still_have_no_reader(files):
    """If a key gained a reader, drop the exemption so the scan protects it."""
    now_read = [k for k in INTENTIONALLY_UNREAD if k in _all_config_keys()
                and _readers(k, files)]
    assert not now_read, (
        f"these keys are exempted but are now read by code: {now_read}; "
        "remove them from INTENTIONALLY_UNREAD"
    )


def test_every_exemption_carries_a_reason():
    empty = [k for k, v in INTENTIONALLY_UNREAD.items() if not (v or "").strip()]
    assert not empty, f"exemptions without a reason: {empty}"


def test_the_scanner_actually_detects_a_known_live_key(files):
    """Meta-meta: prove the scan is not vacuously passing.

    ``max_results_per_doc`` is read in the retrieval path; if the scanner fails
    to find it, the whole check is meaningless.
    """
    assert _readers("max_results_per_doc", files), (
        "scanner found no reader for a key that IS read — the check is broken"
    )


def test_the_scanner_detects_a_known_dead_key(files):
    """And prove the negative case: a key removed this cycle has no reader."""
    assert not _readers("exhaustive_max_chars", files), (
        "the deleted key still has a reader — deletion was incomplete"
    )
