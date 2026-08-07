"""Configuration parsing: OPENLAD_* environment variables drive core paths."""
import importlib

import core.config as config


def test_default_data_dir():
    assert config.DATA_DIR.name == "data"
    assert config.SYSTEM_DB_PATH == config.DATA_DIR / "system.db"
    assert config.TENANTS_DIR == config.DATA_DIR / "tenants"


def test_data_dir_from_env(monkeypatch):
    monkeypatch.setenv("OPENLAD_DATA_DIR", "/tmp/openlad_custom")
    importlib.reload(config)
    try:
        assert str(config.DATA_DIR) == "/tmp/openlad_custom"
        assert str(config.SYSTEM_DB_PATH) == "/tmp/openlad_custom/system.db"
    finally:
        monkeypatch.delenv("OPENLAD_DATA_DIR", raising=False)
        importlib.reload(config)


def test_default_api_key_ttl():
    from core.config import settings

    assert settings.API_KEY_CONFIG["default_ttl_days"] == 90
