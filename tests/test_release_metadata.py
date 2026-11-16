"""Release metadata must describe the configuration that is actually built."""
import re
from pathlib import Path

import yaml

from core.config import settings

ROOT = Path(__file__).resolve().parent.parent


def test_container_release_and_window_metadata():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["openlad"]
    assert service["image"] == "openlad:0.5.0"
    assert service["environment"]["OPENLAD_LLM_MAX_TOKENS"] == "${OPENLAD_LLM_MAX_TOKENS:-32768}"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile
    assert "--require-hashes" in dockerfile


def test_example_window_matches_the_default_budget():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    configured = int(re.search(r"^OPENLAD_LLM_MAX_TOKENS=(\d+)$", example, re.M).group(1))
    assert configured == settings.CONTEXT_CONFIG["llm_max_tokens"]


def test_lock_contains_every_direct_requirement_with_hashes():
    direct = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            direct.append(re.split(r"[>=\[]", line, maxsplit=1)[0].lower().replace("_", "-"))
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lock
    for package in direct:
        assert re.search(rf"^{re.escape(package)}==[0-9]", lock, re.M | re.I), package
