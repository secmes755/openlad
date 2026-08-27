"""Runtime model backend configuration: priority chain, key masking,
auth header propagation, hot reload, and admin gating of the config API.

Local/cloud unification contract under test:
- unset api key resolves to "123" (local backends ignore it; header always
  well-formed for cloud providers)
- DB override > env var > built-in default; empty-string DB tombstone
  clears an override back down the chain
- saving through the route hot-reloads the live ModelClient singleton
- every /admin/models/* route requires the admin role
"""
import asyncio

import pytest

from core.db.system_db import get_system_db
from core.services import model_config


@pytest.fixture()
def clean_model_config(monkeypatch):
    """Fresh registry state: no DB overrides, no env interference."""
    db = get_system_db()
    for f in model_config._FIELDS:
        db.set_config(model_config._DB_PREFIX + f, "")
    for var in ("OPENLAD_LLM_URL", "OPENLAD_LLM_API_KEY", "OPENLAD_LLM_MODEL",
                "OPENLAD_EMB_URL", "OPENLAD_EMB_API_KEY", "OPENLAD_EMB_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield get_system_db
    # teardown: remove overrides again so other tests start clean
    for f in model_config._FIELDS:
        db.set_config(model_config._DB_PREFIX + f, "")


class TestResolutionPriority:
    def test_defaults_when_nothing_set(self, clean_model_config):
        cfg = model_config.get_model_settings()
        assert cfg["llm_api_key"] == "123"          # local default placeholder
        assert cfg["emb_api_key"] == "123"
        assert cfg["llm_url"].startswith("http")

    def test_env_beats_default(self, clean_model_config, monkeypatch):
        monkeypatch.setenv("OPENLAD_LLM_MODEL", "env-model")
        monkeypatch.setenv("OPENLAD_EMB_API_KEY", "env-key")
        cfg = model_config.get_model_settings()
        assert cfg["llm_model"] == "env-model"
        assert cfg["emb_api_key"] == "env-key"

    def test_db_beats_env(self, clean_model_config, monkeypatch):
        monkeypatch.setenv("OPENLAD_LLM_MODEL", "env-model")
        get_system_db().set_config("model_llm_model", "db-model")
        assert model_config.get_model_settings()["llm_model"] == "db-model"

    def test_empty_tombstone_clears_override(self, clean_model_config):
        db = get_system_db()
        db.set_config("model_llm_url", "https://saved.example/v1")
        assert model_config.get_model_settings()["llm_url"] == "https://saved.example/v1"
        # admin "clear" writes empty string -> falls back down the chain
        db.set_config("model_llm_url", "")
        assert model_config.get_model_settings()["llm_url"] != "https://saved.example/v1"


class TestUpdateAndMask:
    def test_update_persists_and_masks(self, clean_model_config):
        view = model_config.update_model_settings({
            "llm_url": "https://api.deepseek.com/v1",
            "llm_api_key": "sk-secret-9999",
            "llm_model": "deepseek-chat",
        })
        assert view["llm_url"] == "https://api.deepseek.com/v1"
        assert view["llm_model"] == "deepseek-chat"
        # secret never returned in full
        assert view["llm_api_key"]["set"] is True
        assert "sk-secret-9999" not in str(view)
        assert view["llm_api_key"]["hint"].endswith("9999")

    def test_mask_of_unset_key(self, clean_model_config):
        m = model_config.mask_key(None)
        assert m == {"set": False, "hint": ""}


class TestClientAuthHeaders:
    def _make_client(self, monkeypatch, captured):
        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "data": [{"embedding": [0.0]}]}
        from core.models.client import ModelClient
        client = ModelClient()
        client.llm_base_url = "https://x.example/v1"
        client.llm_api_key = "sk-abc"
        client.embedding_base_url = "https://x.example/v1"
        client.embedding_api_key = ""

        class FakeSession:
            def post(self, url, **kw):
                captured.update(kw, url=url)
                return FakeResp()
        # _session backs the read-only `session` property; set it directly
        client._session = FakeSession()
        return client

    def test_chat_sends_bearer_from_resolved_key(self, clean_model_config, monkeypatch):
        captured = {}
        c = self._make_client(monkeypatch, captured)
        out = c.generate("hi", temperature=0, max_tokens=8)
        assert out == "ok"
        assert captured["headers"]["Authorization"] == "Bearer sk-abc"

    def test_embed_sends_bearer_when_key_set(self, clean_model_config, monkeypatch):
        captured = {}
        c = self._make_client(monkeypatch, captured)
        c.embedding_api_key = "emb-key-1"
        c.embed("hello")
        assert captured["headers"]["Authorization"] == "Bearer emb-key-1"

    def test_embed_without_key_no_bogus_header(self, clean_model_config, monkeypatch):
        captured = {}
        c = self._make_client(monkeypatch, captured)
        c.embedding_api_key = ""
        c.embed("hello")
        assert "headers" not in captured or "Authorization" not in captured["headers"]

    def test_real_resolution_gives_placeholder_header(self, clean_model_config):
        from core.models.client import ModelClient
        c = ModelClient()  # nothing set -> keys resolve to "123"
        assert ModelClient._auth_headers(c.llm_api_key)["Authorization"] == "Bearer 123"


class TestHotReload:
    def test_reload_swaps_live_singleton_fields(self, clean_model_config):
        from core.models.client import get_model_client
        client = get_model_client()  # force creation with current config
        before = client.llm_base_url
        model_config.update_model_settings({
            "llm_url": "https://hot-swap.example/v1",
            "llm_model": "hot-model",
        })
        assert client.llm_base_url == "https://hot-swap.example/v1"
        assert client.llm_model == "hot-model"
        assert before != client.llm_base_url or before == "https://hot-swap.example/v1"

    def test_route_save_triggers_reload(self, clean_model_config, monkeypatch):
        from core.api.routes import admin as admin_routes
        called = {"n": 0}
        # update_model_settings does a delayed `from ..models.client import
        # reload_model_client`, which resolves this module attribute at call
        # time — so patching here intercepts the route-driven reload.
        monkeypatch.setattr(
            "core.models.client.reload_model_client",
            lambda: called.__setitem__("n", called["n"] + 1) or True,
        )
        monkeypatch.setattr(admin_routes, "_require_admin", lambda: None)
        req = admin_routes.ModelConfigRequest(llm_url="https://route.example/v1")
        result = asyncio.run(admin_routes.put_model_config(req))
        assert result["success"] is True
        assert result["config"]["llm_url"] == "https://route.example/v1"
        assert called["n"] == 1


class TestProbeEndpoint:
    def test_rejects_non_http_scheme(self):
        r = model_config.probe_endpoint("ftp://x")
        assert r["ok"] is False and "http" in r["error"].lower()

    def test_unreachable_reports_error_not_exception(self):
        r = model_config.probe_endpoint("http://127.0.0.1:9/v1", timeout=1.5)
        assert r["ok"] is False and r.get("error")


class TestAdminGating:
    def _ctx_as(self, role):
        from core.tenant.context import TenantContext, set_tenant_context
        set_tenant_context(TenantContext(tenant_id="t", user_id="u1",
                                         username="u", user_role=role))

    def teardown_method(self):
        from core.tenant.context import clear_tenant_context
        clear_tenant_context()

    def test_get_requires_admin(self, clean_model_config):
        from fastapi import HTTPException

        from core.api.routes.admin import get_model_config
        self._ctx_as("user")
        with pytest.raises(HTTPException) as ei:
            import asyncio
            asyncio.run(get_model_config())
        assert ei.value.status_code == 403

    def test_admin_passes_and_gets_masked_view(self, clean_model_config):
        from core.api.routes.admin import get_model_config
        self._ctx_as("admin")
        view = asyncio.run(get_model_config())
        assert "llm_url" in view and hasattr(view["llm_api_key"], "get") \
            and view["llm_api_key"]["set"] in (True, False)

    def test_put_requires_admin(self, clean_model_config):
        from fastapi import HTTPException

        from core.api.routes.admin import ModelConfigRequest, put_model_config
        self._ctx_as("user")
        with pytest.raises(HTTPException) as ei:
            import asyncio
            asyncio.run(put_model_config(ModelConfigRequest(llm_url="http://x/v1")))
        assert ei.value.status_code == 403

    def test_test_endpoint_target_validation(self, clean_model_config):
        from fastapi import HTTPException

        from core.api.routes.admin import ModelTestRequest, test_model_endpoint
        self._ctx_as("admin")
        with pytest.raises(HTTPException) as ei:
            import asyncio
            asyncio.run(test_model_endpoint(ModelTestRequest(target="bogus")))
        assert ei.value.status_code == 400
