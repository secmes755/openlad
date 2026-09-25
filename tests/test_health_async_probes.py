"""#21: /health must not block the event loop on synchronous probes.

health_check is an async route handler, but it called _check_db and two
_check_model_service probes inline — each a synchronous requests.get with
timeout=3, so a dead model service stalled the whole event loop for up to
6 seconds on every health poll. The probes must be dispatched to worker
threads (asyncio.to_thread), the established pattern in this codebase.
"""
import asyncio
import threading

from core.api.routes import health as health_mod


def _patch_probes(monkeypatch, recorded, db_status=None):
    def fake_check(url, name):
        recorded.append((name, threading.current_thread().name))
        return {"status": "ok"}
    monkeypatch.setattr(health_mod, "_check_model_service", fake_check)
    monkeypatch.setattr(health_mod, "_check_db",
                        lambda: db_status or {"status": "ok"})
    monkeypatch.setattr(
        "core.services.model_config.get_model_settings",
        lambda: {"llm_url": "http://x", "emb_url": "http://y"},
    )


def test_model_probes_run_off_event_loop(monkeypatch):
    recorded = []
    _patch_probes(monkeypatch, recorded)
    asyncio.run(health_mod.health_check())
    assert {n for n, _ in recorded} == {"llm", "embedding"}
    for _, thread_name in recorded:
        # asyncio.to_thread workers are named asyncio_N; inline execution
        # (old code) runs on MainThread — the event loop's thread.
        assert thread_name != "MainThread"


def test_db_probe_runs_off_event_loop(monkeypatch):
    seen = []
    def fake_db():
        seen.append(threading.current_thread().name)
        return {"status": "ok"}
    monkeypatch.setattr(health_mod, "_check_db", fake_db)
    monkeypatch.setattr(health_mod, "_check_model_service",
                        lambda url, name: {"status": "ok"})
    monkeypatch.setattr(
        "core.services.model_config.get_model_settings",
        lambda: {"llm_url": "http://x", "emb_url": "http://y"},
    )
    asyncio.run(health_mod.health_check())
    assert seen and seen[0] != "MainThread"


def test_aggregation_unchanged(monkeypatch):
    """A DB error still degrades the overall status."""
    _patch_probes(monkeypatch, [], db_status={"status": "error", "detail": "x"})
    result = asyncio.run(health_mod.health_check())
    assert result["status"] == "degraded"
    assert result["services"]["database"]["status"] == "error"
