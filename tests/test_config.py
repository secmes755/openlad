"""Configuration parsing: OPENLAD_* environment variables drive core paths."""
import importlib
from pathlib import Path

import core.config as config


def test_default_data_dir(monkeypatch):
    # The shared test env isolates the data dir (tests/conftest.py); the
    # "default" assertion must run with the variable explicitly cleared.
    monkeypatch.delenv("OPENLAD_DATA_DIR", raising=False)
    importlib.reload(config)
    try:
        assert config.DATA_DIR.name == "data"
        assert config.SYSTEM_DB_PATH == config.DATA_DIR / "system.db"
        assert config.TENANTS_DIR == config.DATA_DIR / "tenants"
    finally:
        importlib.reload(config)


def test_data_dir_from_env(monkeypatch):
    # Compare parsed Path objects, not the raw string: Path("/tmp/x") keeps
    # its POSIX spelling on Linux but normalizes to a drive-relative form on
    # Windows. Both sides go through identical normalization this way.
    custom = "/tmp/openlad_custom"
    monkeypatch.setenv("OPENLAD_DATA_DIR", custom)
    importlib.reload(config)
    try:
        expected = Path(custom)
        assert config.DATA_DIR == expected
        assert config.SYSTEM_DB_PATH == expected / "system.db"
    finally:
        monkeypatch.delenv("OPENLAD_DATA_DIR", raising=False)
        importlib.reload(config)


def test_default_api_key_ttl():
    from core.config import settings

    assert settings.API_KEY_CONFIG["default_ttl_days"] == 90


def _reload_with_emb_env(monkeypatch, value):
    """Reload core.config with OPENLAD_EMB_MAX_INPUT_TOKENS set (or cleared)."""
    if value is None:
        monkeypatch.delenv("OPENLAD_EMB_MAX_INPUT_TOKENS", raising=False)
    else:
        monkeypatch.setenv("OPENLAD_EMB_MAX_INPUT_TOKENS", value)
    importlib.reload(config)


def test_emb_limits_default_matches_legacy(monkeypatch):
    # Undeclared physical batch: default 2048 (llama.cpp's own default).
    # builder clamps chunks to chunk_size=1600 (< 2150), so chunking behavior
    # is unchanged; only the never-reached ceiling drops 8601 -> 2150.
    _reload_with_emb_env(monkeypatch, None)
    try:
        cfg = config.EMBEDDING_CONFIG
        assert cfg["max_input_tokens"] == 2048
        assert cfg["max_chunk_chars"] == 2150   # min(8601, 2048*1.5*0.7)
        assert cfg["max_embed_chars"] == 2150   # min(6880, 2150)
        assert cfg["chunk_size"] == 1600        # env-isolated test env
        assert cfg["chunk_size"] <= cfg["max_chunk_chars"]
    finally:
        _reload_with_emb_env(monkeypatch, None)


def test_emb_limits_small_batch_deployment(monkeypatch):
    # Deployment with llama-server --batch-size 512: every size limit must
    # collapse into the safe zone so Chinese-density chunks stay embeddable
    # (537 chars at 1.5 chars/token = ~358 tokens < 512).
    _reload_with_emb_env(monkeypatch, "512")
    try:
        cfg = config.EMBEDDING_CONFIG
        assert cfg["max_input_tokens"] == 512
        assert cfg["max_chunk_chars"] == 537
        assert cfg["max_embed_chars"] == 537
        # The builder clamp min(chunk_size, max_chunk_chars) now binds at 537
        assert min(cfg["chunk_size"], cfg["max_chunk_chars"]) == 537
    finally:
        _reload_with_emb_env(monkeypatch, None)


def test_emb_limits_large_batch_deployment(monkeypatch):
    # batch 4096: the context window becomes the binding constraint again.
    _reload_with_emb_env(monkeypatch, "4096")
    try:
        cfg = config.EMBEDDING_CONFIG
        assert cfg["max_chunk_chars"] == 4300   # 4096*1.5*0.7
        assert cfg["max_embed_chars"] == 4300   # min(6880, 4300)
    finally:
        _reload_with_emb_env(monkeypatch, None)
