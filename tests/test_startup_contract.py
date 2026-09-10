"""Startup contract: a core failure aborts startup, it does not degrade silently.

Regression tests for a lifespan that caught every initialisation error and only
logged it. Two consequences were reachable:

* a missing ``OPENLAD_ADMIN_PASSWORD`` logged an error, but the service started
  with no admin user — an instance nobody can administer, reporting healthy;
* a failed ``QueryEngine``/``DocumentIndexBuilder`` left
  ``app.state.query_engine`` unset, so every later ``/query`` returned an opaque
  500 while ``/health`` kept reporting success.

The environment self-check already aborted startup by design; these tests pin
that the same contract applies to the components initialised after it.
"""

import asyncio
import types

import pytest

from core.api import main as api_main
from core.retrieval import engine as engine_mod
from core.services import env_check as env_mod
from core.tenant import auth as auth_mod
from core.tenant import tenant_manager as tm_mod


class _BoomEngine:
    """Stands in for QueryEngine when the retrieval stack cannot initialise."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("engine unavailable")


class _FakeTenantManager:
    def get_tenant(self, tenant_id):
        return types.SimpleNamespace(id=tenant_id)


class _AuthWithAdmin:
    def list_users(self, tenant_id):
        return [types.SimpleNamespace(username="admin")]


class _AuthWithoutUsers:
    def list_users(self, tenant_id):
        return []


def _run_lifespan():
    async def _run():
        async with api_main.lifespan(api_main.app):
            pass

    return asyncio.run(_run())


def test_lifespan_aborts_when_retrieval_engine_fails(monkeypatch):
    monkeypatch.setattr(env_mod, "check_environment", lambda: None)
    monkeypatch.setattr(tm_mod, "get_tenant_manager", lambda: _FakeTenantManager())
    monkeypatch.setattr(auth_mod, "get_auth_manager", lambda: _AuthWithAdmin())
    monkeypatch.setattr(engine_mod, "QueryEngine", _BoomEngine)

    with pytest.raises(RuntimeError, match="Retrieval engine initialization failed"):
        _run_lifespan()


def test_lifespan_aborts_without_admin_password(monkeypatch):
    monkeypatch.setattr(env_mod, "check_environment", lambda: None)
    monkeypatch.setattr(tm_mod, "get_tenant_manager", lambda: _FakeTenantManager())
    monkeypatch.setattr(auth_mod, "get_auth_manager", lambda: _AuthWithoutUsers())
    monkeypatch.delenv("OPENLAD_ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="OPENLAD_ADMIN_PASSWORD is required"):
        _run_lifespan()
