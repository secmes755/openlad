"""Shared pytest configuration.

Middleware-level tests (tests/test_admin_page_access.py) exercise the real
FastAPI app; its auth path creates the system DB at settings.SYSTEM_DB_PATH
on first use. The data dir is isolated here -- pytest_configure runs before
any test module is imported, hence before core.config resolves its paths --
so the working tree stays clean and the tests stay hermetic. Tests that
assert the default path explicitly clear the variable (see test_config.py).
"""
import os


def pytest_configure(config):
    os.environ.setdefault("OPENLAD_DATA_DIR", "/tmp/openlad-ci-test-data")
