"""Runtime model-backend configuration (LLM + Embedding).

Unified local/cloud story: any OpenAI-compatible endpoint is just
(url, api_key, model). Local backends typically ignore the key, so an
unset key resolves to "123" — a well-formed header is always sent and
no special local branch is needed anywhere.

Resolution order per field:
    1. Explicit value stored in system DB   (admin UI "save")
    2. Environment variable                 (.env / docker, bootstrap path)
    3. Built-in default                     (localhost llama.cpp ports)

Values are cached by ModelClient at construction; writes go through
update_model_settings() which persists and triggers reload_model_client()
so the next request hits the new backend without a process restart.

DB keys are namespaced `model_<field>` in the existing system_config
table (plaintext by design for this LAN-deployed tool; documented).
"""
import logging
import os
import threading

logger = logging.getLogger(__name__)

_FIELDS = (
    "llm_url", "llm_api_key", "llm_model",
    "emb_url", "emb_api_key", "emb_model",
)
_DB_PREFIX = "model_"
_DEFAULT_KEY = "123"

_lock = threading.Lock()


def _env_fallbacks() -> dict:
    """Field -> env-var -> built-in default, captured from config/env."""
    from ..config import settings
    return {
        "llm_url": (os.environ.get("OPENLAD_LLM_URL"), settings.LLM_BASE_URL),
        "llm_api_key": (os.environ.get("OPENLAD_LLM_API_KEY"), _DEFAULT_KEY),
        "llm_model": (os.environ.get("OPENLAD_LLM_MODEL"), ""),
        "emb_url": (os.environ.get("OPENLAD_EMB_URL"), settings.EMBEDDING_API_BASE),
        "emb_api_key": (os.environ.get("OPENLAD_EMB_API_KEY"), _DEFAULT_KEY),
        "emb_model": (os.environ.get("OPENLAD_EMB_MODEL"), ""),
    }


def _db_get(field: str) -> str | None:
    try:
        from ..db.system_db import get_system_db
        val = get_system_db().get_config(_DB_PREFIX + field)
    except Exception as e:
        logger.debug(f"[MODEL_CFG] db read failed for {field} (non-fatal): {e}")
        return None
    return val if val not in (None, "") else None


def get_model_settings() -> dict[str, str]:
    """Resolve all six fields through the priority chain."""
    fb = _env_fallbacks()
    out = {}
    for f in _FIELDS:
        env_val, default = fb[f]
        out[f] = _db_get(f) or env_val or default
    return out


def mask_key(key: str | None) -> dict:
    """API-key wire shape: never return the full secret."""
    if not key:
        return {"set": False, "hint": ""}
    return {"set": True, "hint": ("..." + key[-4:]) if len(key) > 4 else "..."}


def public_view(resolved: dict) -> dict:
    """Resolved settings shaped for the admin UI (keys masked)."""
    return {
        "llm_url": resolved["llm_url"],
        "llm_model": resolved["llm_model"],
        "llm_api_key": mask_key(resolved["llm_api_key"]),
        "emb_url": resolved["emb_url"],
        "emb_model": resolved["emb_model"],
        "emb_api_key": mask_key(resolved["emb_api_key"]),
    }


_UPDATABLE = set(_FIELDS)


def update_model_settings(updates: dict) -> dict:
    """Persist provided fields (empty string clears -> falls back to env),
    then hot-reload the model client. Returns the new public view."""
    clean = {}
    for k, v in (updates or {}).items():
        if k in _UPDATABLE and isinstance(v, str):
            clean[k] = v.strip()
        elif k in _UPDATABLE and v is None:
            clean[k] = ""
    from ..db.system_db import get_system_db
    db = get_system_db()
    with _lock:
        for k, v in clean.items():
            if v == "":
                # Clearing stores empty which _db_get treats as unset, so a
                # tombstone write keeps an admin "clear" durable while the
                # read path falls back to env/default identically.
                db.set_config(_DB_PREFIX + k, "")
            else:
                db.set_config(_DB_PREFIX + k, v)
        resolved = get_model_settings()
    # Hot-reload outside the DB transaction but inside request handling;
    # query serialization lock on the API side makes attr swaps safe.
    from ..models.client import reload_model_client
    reload_model_client()
    logger.info(
        "[MODEL_CFG] updated: llm=%s model=%r emb=%s model=%r (keys %s/%s)",
        resolved["llm_url"], resolved["llm_model"],
        resolved["emb_url"], resolved["emb_model"],
        "set" if clean.get("llm_api_key") else "kept",
        "set" if clean.get("emb_api_key") else "kept",
    )
    return public_view(resolved)


def probe_endpoint(url: str, api_key: str = "", timeout: float = 8.0) -> dict:
    """Reachability probe usable BEFORE saving: lists model ids so the
    operator can copy the exact model name an OpenAI-compatible endpoint
    registered under."""
    import requests
    url = (url or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(f"{url}/models", headers=headers, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if resp.status_code != 200:
        return {"ok": False, "status_code": resp.status_code,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    models = []
    try:
        data = resp.json().get("data", [])
        models = [m.get("id", "") for m in data if isinstance(m, dict)]
    except Exception:
        pass
    return {"ok": True, "status_code": 200, "models": models[:20]}
