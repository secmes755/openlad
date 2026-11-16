"""Public smoke scripts must authenticate and validate answer content."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

# The scripts are bash; on hosts without it the syntax check cannot run. The
# content assertions above still apply there.
requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is not available on this host")


def test_public_smoke_script_has_valid_syntax_and_uses_supplied_key():
    script = Path(__file__).resolve().parent.parent / "scripts" / "smoke.sh"
    text = script.read_text(encoding="utf-8")
    assert "${KEY}" in text
    assert "Authorization: ${AUTH_SCHEME} ${KEY}" in text
    assert "Authorization: ***" not in text
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_default_smoke_is_small_and_has_answer_contracts():
    root = Path(__file__).resolve().parent.parent
    manifest = root / "fixtures" / "smoke" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["documents"]
    for document in data["documents"]:
        assert (manifest.parent / document["file"]).is_file()
        assert document["queries"]
        for query in document["queries"]:
            assert query["contains"]
    assert "fixtures/smoke/manifest.json" in (root / "scripts" / "smoke.sh").read_text(encoding="utf-8")


def test_docker_verify_uses_login_key_and_checks_answer_content():
    script = Path(__file__).resolve().parent.parent / "scripts" / "docker_verify.sh"
    text = script.read_text(encoding="utf-8")
    assert "Bearer ***" not in text
    assert "Authorization: Bearer $KEY" in text
    assert "expected specification" in text
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(script)], check=True)
