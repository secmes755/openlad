"""The public smoke script must use the key it requires from the caller."""
import subprocess
from pathlib import Path


def test_public_smoke_script_has_valid_syntax_and_uses_supplied_key():
    script = Path(__file__).resolve().parent.parent / "scripts" / "smoke.sh"
    text = script.read_text(encoding="utf-8")
    assert "${KEY}" in text
    assert '"$HEADER_FLAG" "Authorization: ${AUTH_SCHEME} ${KEY}"' in text
    subprocess.run(["bash", "-n", str(script)], check=True)
