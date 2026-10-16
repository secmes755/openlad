"""The tenant root the builder uses is the one the caller redirected.

The builder resolves the tenant root from the module-level
``config.TENANTS_DIR``. Patching it must actually move the builder — the
obvious redirect (``config.TENANTS_DIR = <tmp>``) used to be applied to the
``Settings`` class instead, which the builder did not read, so a redirected
root was silently ignored. The module global is also the name that stays
consistent across ``importlib.reload`` (used by ``tests/test_config.py``):
a reload rebinds the global and rebuilds both the class and the instance.
"""
from pathlib import Path

import pytest

import core.config as config
from core.config import settings


@pytest.fixture
def redirected_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TENANTS_DIR", tmp_path)
    return tmp_path


def test_the_builder_follows_a_redirected_root(redirected_root):
    resolved = settings.get_tenant_data_dir("admin")
    assert resolved.parent == redirected_root.resolve()
    assert resolved.name == "admin"


def test_the_db_path_helper_follows_too(redirected_root):
    path = settings.get_tenant_db_path("admin")
    assert path.parent.parent == redirected_root.resolve()
    assert path.name == "metadata.db"


def test_escape_rejection_still_applies_after_a_redirect(redirected_root):
    """Redirecting the root must not create a window where the check is off."""
    for bad in ["..", "/tmp/evil", "../x", "a/b"]:
        with pytest.raises(ValueError):
            settings.get_tenant_data_dir(bad)


def test_an_escaped_candidate_is_measured_against_the_redirected_root(
        redirected_root):
    """The prefix check compares against the *current* root, not the original."""
    root = Path(redirected_root).resolve()
    assert root in settings.get_tenant_data_dir("admin").parents
