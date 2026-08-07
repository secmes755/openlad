"""Login rate-limit window logic (pure state machine, no network or services)."""
import time

from core.api.middleware.rate_limit import RateLimitMiddleware


def _mw():
    return RateLimitMiddleware(app=None)


def test_allows_up_to_limit():
    mw = _mw()
    for _ in range(5):
        assert mw._is_allowed("k", 5) is True


def test_rejects_once_limit_reached():
    mw = _mw()
    for _ in range(5):
        mw._is_allowed("k", 5)
    assert mw._is_allowed("k", 5) is False


def test_window_expiry_resets():
    mw = _mw()
    for _ in range(5):
        mw._is_allowed("k", 5)
    # Force all records outside the 60s window
    mw._records["k"] = [time.time() - 61]
    assert mw._is_allowed("k", 5) is True


def test_keys_are_isolated():
    mw = _mw()
    for _ in range(5):
        mw._is_allowed("a", 5)
    assert mw._is_allowed("b", 5) is True


def test_custom_window():
    mw = _mw()
    assert mw._is_allowed("k", 2, window_seconds=60) is True
    assert mw._is_allowed("k", 2, window_seconds=60) is True
    assert mw._is_allowed("k", 2, window_seconds=60) is False
