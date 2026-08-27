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
