"""Lazy, tenant-local PDF page image rendering and placeholder detection."""
import logging
import os
import tempfile
import threading
import weakref
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

_PLACEHOLDER_SIZE = (800, 1000)
_SMALL_FILE_BYTES = 20_000
_TARGET_LOCKS = weakref.WeakValueDictionary()
_TARGET_LOCKS_GUARD = threading.Lock()


def _target_lock(target: Path) -> threading.Lock:
    key = str(target.absolute())
    with _TARGET_LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault(key, threading.Lock())


def document_supports_page_images(document: dict | None) -> bool:
    """Only a real source PDF has renderable document pages."""
    source = (document or {}).get('original_path') or ''
    return Path(source).suffix.lower() == '.pdf'


def is_placeholder_image(path: str | Path) -> bool:
    """Return True for missing, unreadable, or legacy solid-white placeholders."""
    target = Path(path)
    if target.is_file() and target.with_name(target.name + '.rendered').is_file():
        return False
    try:
        if target.stat().st_size > _SMALL_FILE_BYTES:
            return False
        with Image.open(target) as image:
            if tuple(image.size) != _PLACEHOLDER_SIZE:
                return False
            low, high = image.convert('L').getextrema()
            return low == high
    except (OSError, ValueError):
        return True


def ensure_page_image(pdf_path: str, page_num: int,
                      target_path: str | Path, dpi: int = 150) -> bool:
    """Render one missing/placeholder page and atomically cache it.

    The per-target lock covers the second placeholder check, rendering, and
    replacement, so concurrent requests for the same target cannot render or
    replace it twice. Pdfium itself is serialized at the parser's rendering
    layer. Unique temporary names prevent partial files from becoming visible.
    """
    target = Path(target_path)
    if not is_placeholder_image(target):
        return False
    source = Path(pdf_path)
    if page_num < 1 or source.suffix.lower() != '.pdf' or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)

    with _target_lock(target):
        if not is_placeholder_image(target):
            return False
        temporary = None
        try:
            from .parser import DocumentParser
            image = DocumentParser()._render_with_pdfium(
                str(source), [page_num], dpi,
            ).get(page_num)
            if image is None:
                return False
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f'.{target.name}.',
                suffix='.tmp', delete=False,
            ) as handle:
                temporary = Path(handle.name)
            image.save(temporary, 'PNG')
            os.replace(temporary, target)
            try:
                target.with_name(target.name + '.rendered').write_text('1\n')
            except OSError:
                # A missing marker only causes a later redundant render; the
                # freshly written image is still safe to serve now.
                pass
            return True
        except Exception as exc:
            logger.warning(f"Lazy page render failed p{page_num}: {exc}")
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
