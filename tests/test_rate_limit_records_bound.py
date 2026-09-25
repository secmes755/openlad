"""The rate-limit middleware must bound the size of its _records dict.

#13: every distinct caller (API-key hash, client IP, login username) added
a permanent entry to RateLimitMiddleware._records. Timestamps were pruned
only for the key being accessed, so entries for callers that never return
accumulated for the whole process lifetime. The middleware now evicts
semantically dead keys (window fully expired) once the dict grows past a
cap, and falls back to evicting least-recently-active callers.
"""

import time

from starlette.applications import Starlette

from core.api.middleware.rate_limit import RateLimitMiddleware


def _middleware() -> RateLimitMiddleware:
    return RateLimitMiddleware(app=Starlette())


def test_stale_keys_are_evicted_once_over_cap(monkeypatch):
    monkeypatch.setattr(RateLimitMiddleware, "_MAX_TRACKED_KEYS", 50)
    mw = _middleware()
    long_ago = time.time() - 3600
    for i in range(200):
        mw._records[f"ip:gone-{i}"] = [long_ago]

    mw._is_allowed("ip:active", 5)

    assert len(mw._records) <= 51, (
        f"dict kept growing: {len(mw._records)} entries after cap 50"
    )
    assert "ip:active" in mw._records


def test_eviction_prefers_least_recently_active_callers(monkeypatch):
    monkeypatch.setattr(RateLimitMiddleware, "_MAX_TRACKED_KEYS", 50)
    mw = _middleware()
    now = time.time()
    # 100 callers, all inside the live window, oldest activity first
    for i in range(100):
        mw._records[f"ip:caller-{i:03d}"] = [now - 50 + i * 0.1]

    mw._is_allowed("ip:newest", 5)

    assert len(mw._records) <= 51
    # The most recently active callers must survive; the idlest must not.
    assert "ip:caller-099" in mw._records
    assert "ip:caller-000" not in mw._records


def test_rate_decisions_unchanged_below_cap():
    mw = _middleware()
    for _ in range(3):
        assert mw._is_allowed("ip:a", 3)
    assert not mw._is_allowed("ip:a", 3), "limit no longer enforced"
