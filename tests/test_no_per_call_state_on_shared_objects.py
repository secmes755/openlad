"""Source guard: shared objects must not carry per-call state.

The defect this guards against has recurred in this codebase: a value that
belongs to one call (a reply's finish reason, an OCR page's trim report, a
JSON parse error, "the current tenant") is parked on an object that is
shared across tenants, requests and threads, and the caller reads it back
after the call returns. Under concurrency the value read belongs to
whichever call finished last, so a caller acts on another call's outcome.
This was fixed four times over (page classifier tally, OCR trim report,
model-client finish reason / JSON error, builder tenant) — which is exactly
the signal that a behavioural test is not enough: it catches the instance
in front of it, not the next one someone adds.

So this is a static check over the shared classes' source. It fails if a
per-call field is assigned on one of them. Adding a legitimate shared
field (a pool, a lock, a config value) is fine — the patterns below name
only the "last/current X" shapes that mean per-call state. If you genuinely
need one of these names on a shared class, that is the moment to stop and
ask whether the value should be returned instead; see the four commits
this guard protects.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Classes that are shared across tenants / requests / threads, and the file
# each lives in. Keep this list short and deliberate.
SHARED = {
    "core/models/client.py": "ModelClient",
    "core/ingestion/parser.py": "DocumentParser",
    "core/ingestion/builder.py": "DocumentIndexBuilder",
    "core/ingestion/layout/page_classifier.py": "PageClassifier",
}

# Field names that describe "what just happened in this call", which must
# not live on a shared object. Deliberately narrow: only the shapes that
# have actually gone wrong, so the guard does not cry wolf on legitimate
# shared fields (``_session``, ``_lock``, ``llm_model``, ``_ingest_locks``).
PER_CALL_FIELD = re.compile(
    r"\bself\.("
    r"last_[a-z_]+"          # last_finish_reason, last_ocr_trim, ...
    r"|_stats"               # the page classifier's counter
    r"|_last_[a-z_]+"
    r"|current_[a-z_]+"
    r"|_current_[a-z_]+"
    r")\s*=(?!=)"
)

# Documented exceptions. Each must name the file, the field and the reason
# it is still acceptable, and be paired with a note of what replaces it.
# This is deliberately annoying to extend: an entry here is a known wart,
# not a pass. Keep the list empty when you can.
ALLOWED = {
    # The legacy client methods (generate, generate_json) still set these
    # two as a compatibility mirror. They are never read by any caller in
    # this repo (checked); the new APIs (ChatResult / generate_json_strict)
    # carry the same facts as values. Remove them when the legacy methods
    # are retired.
    "core/models/client.py": {"last_finish_reason", "last_json_error"},
}


def _class_body(source: str, class_name: str) -> str:
    """Return the source of a class body, from ``class X`` to the next
    top-level ``class``/``def`` or end of file.

    Good enough for this check: the shared classes are all top-level and
    none is followed by another top-level statement inside the slice that
    matters. We stop at the next line that starts at column 0 with
    ``class`` or ``def``.
    """
    lines = source.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^class {re.escape(class_name)}\b", line):
            start = i
            break
    if start is None:
        return ""
    body = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r"^(class |def )", line):
            break
        body.append(line)
    return "\n".join(body)


@pytest.mark.parametrize("relpath,class_name", sorted(SHARED.items()))
def test_shared_class_has_no_per_call_state_fields(relpath, class_name):
    path = REPO / relpath
    assert path.exists(), f"{relpath} not found — update SHARED in this guard"
    body = _class_body(path.read_text(encoding="utf-8"), class_name)
    assert body, f"could not locate class {class_name} in {relpath}"

    allowed = ALLOWED.get(relpath, set())

    def _is_allowed(line: str) -> bool:
        m = PER_CALL_FIELD.search(line)
        if not m:
            return False
        return m.group(1) in allowed

    offenders = [
        (i + 1, line.strip())
        for i, line in enumerate(body.splitlines())
        if PER_CALL_FIELD.search(line)
        and not line.strip().startswith("#")
        and not _is_allowed(line)
    ]
    assert not offenders, (
        f"{class_name} ({relpath}) assigns per-call state on a shared object:\n"
        + "\n".join(f"  {ln}: {txt}" for ln, txt in offenders)
        + "\n\nA value that belongs to one call must be returned to that call, "
          "not parked on an object every tenant and thread shares — the reader "
          "would get whichever call finished last. See "
          "tests/test_model_client_call_state_is_not_shared.py and the OCR/"
          "classifier/builder equivalents."
    )


def test_the_guard_actually_catches_the_pattern():
    """Meta-test: the regex must match the shapes it claims to.

    Without this, a typo in PER_CALL_FIELD would silently disable the
    guard and every other test here would still pass.
    """
    assert PER_CALL_FIELD.search("        self.last_finish_reason = None")
    assert PER_CALL_FIELD.search("        self._stats = {}")
    assert PER_CALL_FIELD.search("        self.last_ocr_trim = trim_info")
    assert PER_CALL_FIELD.search("        self._last_seen = 1")
    assert PER_CALL_FIELD.search("        self.current_doc = d")
    # Legitimate shared fields must NOT match.
    assert not PER_CALL_FIELD.search("        self._session = None")
    assert not PER_CALL_FIELD.search("        self._lock = threading.Lock()")
    assert not PER_CALL_FIELD.search("        self.llm_model = cfg['m']")
    # A comparison is not an assignment.
    assert not PER_CALL_FIELD.search("        if self.last_x == 1:")


def test_the_allow_list_is_not_stale():
    """Every allow-listed field must still exist in its file.

    If the legacy attributes are removed, their exception entries become
    dead and this test fails, prompting the list back to empty.
    """
    for relpath, fields in ALLOWED.items():
        source = (REPO / relpath).read_text(encoding="utf-8")
        for field in fields:
            assert f"self.{field}" in source, (
                f"ALLOWED lists {field} for {relpath}, but it no longer "
                "appears there — remove the stale exception"
            )
