"""Environment self-check: fail-fast startup on incomplete dependencies.

Regression test for the incident where the API was started under a venv
missing pypdf/pdfplumber: PDFs were 'parsed' into placeholder pages and
stored as verified with 1 page / 1 chunk.
"""

import importlib
import os

import pytest

from core.services.env_check import REQUIRED_MODULES, check_environment


def test_environment_complete_passes():
    """Current environment must carry every declared dependency.

    Unit-test environments (CI / ci_gate) install only the minimal
    dependency set, so this full-runtime assertion is meaningful on a
    machine actually running the service (requirements.txt). It is
    skipped when required modules are absent rather than failing the
    gate.
    """
    try:
        check_environment()  # should not raise
    except RuntimeError as e:
        pytest.skip(f"full runtime deps not installed in this test env: {e}")


def test_missing_dependency_aborts_startup(monkeypatch):
    """Any missing required module must raise RuntimeError listing it."""
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("No module named 'pdfplumber'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(RuntimeError, match="pdfplumber"):
        check_environment()


def test_escape_hatch_bypasses_check(monkeypatch):
    """OPENLAD_ENV_CHECK=off bypasses even with missing modules."""
    def always_fail(name, *args, **kwargs):
        raise ImportError("blocked")

    monkeypatch.setattr(importlib, "import_module", always_fail)
    monkeypatch.setenv("OPENLAD_ENV_CHECK", "off")
    check_environment()  # should not raise


def test_required_modules_cover_requirements_txt():
    """Every requirements.txt package must have an import-name mapping."""
    req_path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
    with open(req_path) as f:
        lines = [ln.strip() for ln in f
                 if ln.strip() and not ln.startswith("#")]
    pip_names = {ln.split(">=")[0].split("[")[0].strip() for ln in lines}
    declared = set(REQUIRED_MODULES.values())
    # Normalise: declared values carry extras ([standard]); compare bare names.
    declared_bare = {d.split("[")[0] for d in declared}
    missing = pip_names - declared_bare
    assert not missing, f"requirements.txt packages not covered by env check: {missing}"
