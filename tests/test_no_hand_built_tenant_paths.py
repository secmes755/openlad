"""No module may build a tenant path by hand.

``get_tenant_data_dir`` and its siblings are the single validated way to turn
a tenant id into a filesystem path. The diagnostic routes joined the path
themselves (``settings.TENANTS_DIR / tenant_id / "metadata.db"``), which
accepts traversal and absolute-path ids — the same hole the path builder was
written to close, reached through a different door.

A behavioural test cannot catch a *new* bypass cheaply, so this is a source
guard: it fails when a module reintroduces a hand-built join. The guard is the
point — the same class of defect recurred once already.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ["core"]

# A hand-built tenant path: TENANTS_DIR (or the id-join idiom) used with "/".
BYPASS = re.compile(r"TENANTS_DIR\s*/|os\.path\.join\(\s*[^,]*TENANTS_DIR")


def _iter_python_files():
    for d in SCANNED_DIRS:
        yield from (REPO_ROOT / d).rglob("*.py")


def test_only_config_builds_tenant_paths():
    offenders = []
    for path in _iter_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "core/config.py":
            # The builder itself lives here and legitimately joins.
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if BYPASS.search(line):
                offenders.append(f"{rel}:{lineno}: {stripped}")

    assert not offenders, (
        "tenant paths must be built with settings.get_tenant_*_path(); "
        "hand-built joins reintroduce the traversal/absolute-path hole:\n"
        + "\n".join(offenders)
    )
