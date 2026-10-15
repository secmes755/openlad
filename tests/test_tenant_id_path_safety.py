"""A tenant id must not be able to point outside the tenants directory.

``get_tenant_data_dir`` builds a filesystem path from a URL path parameter
(``DELETE /admin/tenants/{tenant_id}``), and a hard delete passes the result to
``shutil.rmtree``. Three shapes used to escape the tenants root because a plain
``Path`` join accepts them:

- ``"../../x"`` — relative traversal
- ``"/tmp/x"`` — an absolute component replaces the entire left-hand prefix,
  since ``Path("/a") / "/tmp/x" == Path("/tmp/x")``
- ``".."`` — resolves to ``data/``, so a recursive delete would remove every
  tenant at once

The check rejects on shape, not only on the resolved path, so it also holds when
the target does not exist yet.
"""
import pytest

from core.config import settings


def test_ordinary_ids_still_resolve():
    """The ids actually present in data/tenants must keep working."""
    for tid in ("admin", "benchmark", "default", "u_t3a",
                "c00b8af5-521c-4929-adc6-a29b834c1724"):
        resolved = settings.get_tenant_data_dir(tid)
        assert resolved.name == tid
        assert resolved.parent.name == "tenants"


def test_relative_traversal_is_rejected():
    with pytest.raises(ValueError):
        settings.get_tenant_data_dir("../../../../tmp/evil")
    with pytest.raises(ValueError):
        settings.get_tenant_data_dir("sub/child")


def test_absolute_path_is_rejected():
    """Path("/a") / "/tmp/x" drops the prefix entirely, so this must be refused."""
    with pytest.raises(ValueError):
        settings.get_tenant_data_dir("/tmp/evil")


@pytest.mark.parametrize("tid", ["..", ".", "", "   ", "a b"])
def test_empty_and_dot_ids_are_rejected(tid):
    """``..`` resolves to data/ itself — the parent of every tenant."""
    with pytest.raises(ValueError):
        settings.get_tenant_data_dir(tid)


def test_non_string_ids_are_rejected():
    for bad in (None, 123, ["x"], {"x": 1}):
        with pytest.raises(ValueError):
            settings.get_tenant_data_dir(bad)  # type: ignore[arg-type]


def test_overlong_id_is_rejected():
    with pytest.raises(ValueError):
        settings.get_tenant_data_dir("a" * 65)


def test_every_derived_path_stays_under_the_tenant_dir():
    """The sibling helpers must inherit the guard rather than re-join by hand."""
    tid = "benchmark"
    base = settings.get_tenant_data_dir(tid)
    for path in (settings.get_tenant_db_path(tid),
                 settings.get_tenant_vec_db_path(tid),
                 settings.get_tenant_documents_dir(tid),
                 settings.get_tenant_images_dir(tid)):
        assert base in path.parents

    for path_fn in (settings.get_tenant_db_path,
                    settings.get_tenant_vec_db_path,
                    settings.get_tenant_documents_dir,
                    settings.get_tenant_images_dir):
        with pytest.raises(ValueError):
            path_fn("../../evil")
