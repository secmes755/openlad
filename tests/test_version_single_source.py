"""The application version has exactly one home.

It used to be the literal "1.0.0" in four places across the API layer while the
project was in fact releasing 0.4.x, so ``/health`` and the OpenAPI schema
misreported the running release. These tests pin the single source and the
absence of the literals that made it drift.
"""
import re
from pathlib import Path

import pytest

from core.version import __version__

ROOT = Path(__file__).resolve().parent.parent
FORMER_LITERAL_SITES = ("core/api/main.py", "core/api/routes/health.py")


def test_version_looks_like_a_release():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), (
        f"__version__ must be a plain x.y.z release string, got {__version__!r}"
    )


def test_fastapi_schema_version_comes_from_the_single_source():
    from core.api.main import app

    assert app.version == __version__


@pytest.mark.parametrize("rel", FORMER_LITERAL_SITES)
def test_no_hardcoded_version_literal(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "1.0.0" not in src, f"{rel} still hardcodes a version literal"
    assert "__version__" in src, f"{rel} no longer reads the single source"
