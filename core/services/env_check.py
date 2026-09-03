"""Startup environment self-check.

OpenLAD's correctness depends on the Python environment carrying every
dependency declared in requirements.txt. A wrong interpreter (e.g. a venv
with fastapi but without pdfplumber) lets the API boot and then silently
degrades ingestion (parser falls back to placeholder pages, producing
'verified' documents with near-zero content). This module fail-fasts at
startup so a broken environment can never serve traffic.

Set OPENLAD_ENV_CHECK=off to bypass (escape hatch; default is strict).
"""

import importlib
import logging
import os

logger = logging.getLogger(__name__)

# import-name -> pip package name (only where they differ)
REQUIRED_MODULES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "pydantic": "pydantic",
    "multipart": "python-multipart",
    "dotenv": "python-dotenv",
    "duckdb": "duckdb",
    "sqlite_vec": "sqlite-vec",
    "pypdf": "pypdf",
    "pdfplumber": "pdfplumber",
    "pandas": "pandas",
    "openpyxl": "openpyxl",
    "pptx": "python-pptx",
    "docx": "python-docx",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "pdf2image": "pdf2image",
    "numpy": "numpy",
    "cv2": "opencv-python",
    "httpx": "httpx",
    "requests": "requests",
    "yaml": "PyYAML",
    "jinja2": "Jinja2",
    "bcrypt": "bcrypt",
    "psutil": "psutil",
}


def check_environment() -> None:
    """Verify all declared dependencies are importable.

    Raises RuntimeError listing every missing module when the environment
    is incomplete. Bypass with OPENLAD_ENV_CHECK=off.
    """
    if os.environ.get("OPENLAD_ENV_CHECK", "").lower() == "off":
        logger.warning("[ENV_CHECK] disabled via OPENLAD_ENV_CHECK=off")
        return

    missing = []
    for module, package in REQUIRED_MODULES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if missing:
        raise RuntimeError(
            "OpenLAD environment self-check FAILED — missing dependencies: "
            + ", ".join(sorted(missing))
            + ". The service refuses to start because ingestion would silently "
            "degrade (e.g. unparsed PDFs marked verified). Fix with: "
            "pip install -r requirements.txt  (in the correct venv), or bypass "
            "with OPENLAD_ENV_CHECK=off if you know what you are doing."
        )

    logger.info(f"[ENV_CHECK] all {len(REQUIRED_MODULES)} required dependencies importable")
