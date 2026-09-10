"""requirements-ci.txt must cover every module-level import under core/.

The CI unit job runs ``pytest``, which imports whatever the tests touch. When a
dependency is missing from the CI set the job fails at collection — or, worse,
passes on a runner image that happens to preinstall the package, so the same
commit is green or red depending on which runner it lands on. That is exactly how
numpy and Pillow went missing: five modules under core/ import numpy at module
level and four import PIL, while the dependency list carried neither.

These tests close the class off. They parse core/ for module-level third-party
imports, ignore the ones that are optional by construction (inside try/except, or
nested inside a function), and require the rest to be declared for CI and present
in the environment. A new module that hard-imports a new package now fails here
instead of five commits later on a runner.
"""
import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
CI_REQUIREMENTS = ROOT / "requirements-ci.txt"

# import name -> distribution name, where the two differ
IMPORT_TO_DIST = {
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "multipart": "python-multipart",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "sqlite_vec": "sqlite-vec",
}
INTERNAL = {"core", "industries", "main", "tests", "scripts"}


def _guarded_lines(tree: ast.AST) -> set[int]:
    """Line numbers of imports inside a try/except: optional by construction."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    lines.add(child.lineno)
    return lines


def _main_guard_lines(tree: ast.AST) -> set[int]:
    """Imports under `if __name__ == "__main__":` never run on import."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        try:
            condition = ast.unparse(node.test)
        except Exception:                                   # pragma: no cover
            continue
        if "__main__" not in condition or "__name__" not in condition:
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                lines.add(child.lineno)
    return lines


def _module_level_imports() -> dict[str, set[str]]:
    """Third-party roots imported at module level, outside any try/except."""
    found: dict[str, set[str]] = {}
    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded = _guarded_lines(tree) | _main_guard_lines(tree)
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if node.lineno in guarded:
                continue
            if isinstance(node, ast.ImportFrom):
                if node.level:                      # relative import -> internal
                    continue
                root = (node.module or "").split(".")[0]
            else:
                root = node.names[0].name.split(".")[0]
            if not root or root in INTERNAL or root in sys.stdlib_module_names:
                continue
            found.setdefault(root, set()).add(rel)
    return found


def _declared_for_ci() -> set[str]:
    declared = set()
    for line in CI_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        spec = line.split("#", 1)[0].strip()
        if spec:
            declared.add(re.split(r"[<>=!~ ]", spec, maxsplit=1)[0].lower())
    return declared


def test_every_module_level_import_is_declared_for_ci():
    declared = _declared_for_ci()
    missing = {
        module: (IMPORT_TO_DIST.get(module, module).lower(), sorted(files))
        for module, files in _module_level_imports().items()
        if IMPORT_TO_DIST.get(module, module).lower() not in declared
    }
    assert not missing, (
        "core/ imports these at module level, but requirements-ci.txt does not "
        "declare them, so the CI unit job cannot import those modules: "
        + "; ".join(f"{mod} (dist {dist}) <- {', '.join(files[:3])}"
                    for mod, (dist, files) in sorted(missing.items()))
    )


def test_every_module_level_import_is_installed_here():
    absent = sorted(mod for mod in _module_level_imports()
                    if importlib.util.find_spec(mod) is None)
    assert not absent, (
        "this environment cannot import modules that CI must be able to import: "
        f"{absent}. Install the CI set: pip install -r requirements-ci.txt"
    )
