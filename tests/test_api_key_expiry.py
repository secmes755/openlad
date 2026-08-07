"""API key TTL semantics: None -> configured default, <=0 -> never, >0 -> now+ttl."""
from datetime import datetime, timedelta

from core.tenant.auth import AuthManager


def _within(dt, lo_days, hi_days):
    diff = dt - datetime.now()
    return timedelta(days=lo_days) < diff < timedelta(days=hi_days)


def test_none_uses_configured_default(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "API_KEY_CONFIG", {"default_ttl_days": 90})
    exp = AuthManager._compute_api_key_expiry(None)
    assert exp is not None
    assert _within(exp, 89, 91)


def test_zero_never_expires():
    assert AuthManager._compute_api_key_expiry(0) is None


def test_negative_never_expires():
    assert AuthManager._compute_api_key_expiry(-7) is None


def test_positive_ttl():
    exp = AuthManager._compute_api_key_expiry(30)
    assert exp is not None
    assert _within(exp, 29, 31)
