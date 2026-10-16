"""Every tenant-id-to-path join goes through the one validated builder.

The id arrives as a URL path parameter or a query parameter, and the joined
path is handed to ``sqlite3.connect``. A bare ``Path`` join accepts three
shapes that escape the tenants root — relative traversal, an absolute path
(which replaces the whole prefix, since ``Path("/a") / "/tmp/x" ==
Path("/tmp/x")``), and ``..`` — and the diagnostic routes used to join it that
way while the rest of the codebase went through ``get_tenant_data_dir``.

These tests pin the builder used by the routes, not the route internals: the
routes have exactly one way to turn an id into a path.
"""
import pytest

from core.config import settings

ESCAPING_IDS = [
    "/tmp/evil",           # absolute — replaces the whole prefix
    "../../../tmp/evil",   # relative traversal
    "..",                  # resolves to data/ itself
    ".",                   # the tenants dir itself
    "a/b",                 # separator
    "a\\b",                # backslash
    "",                    # empty
    "a" * 65,              # past the id ceiling
    "张三",                 # non-ASCII
    "a b",                 # whitespace
]


@pytest.mark.parametrize("tenant_id", ESCAPING_IDS)
def test_the_path_builder_refuses_every_escaping_id(tenant_id):
    with pytest.raises(ValueError):
        settings.get_tenant_db_path(tenant_id)


@pytest.mark.parametrize("tenant_id", ["admin", "benchmark", "u_t3a",
                                       "c00b8af5-521c-4929-adc6-a29b834c1724"])
def test_real_tenant_ids_still_resolve(tenant_id):
    path = settings.get_tenant_db_path(tenant_id)
    assert path.name == "metadata.db"
    assert path.parent.name == tenant_id
    # The resolved parent must be a direct child of the tenants root.
    assert path.parent.parent == settings.TENANTS_DIR.resolve()


@pytest.mark.parametrize("tenant_id", ["/tmp/evil", "../../../tmp/evil", "..",
                                       ".", "a/b", "a\\b", "", "a" * 65,
                                       "张三", "a b", "/etc/passwd"])
def test_the_joined_path_cannot_leave_the_tenants_root(tenant_id):
    """The property that matters, stated as such.

    A bare ``Path`` join would place '/tmp/evil' outside the root; the builder
    must not, for any input.
    """
    with pytest.raises(ValueError):
        settings.get_tenant_db_path(tenant_id)
    # And a legal one stays inside.
    root = settings.TENANTS_DIR.resolve()
    assert root in settings.get_tenant_db_path("admin").parents


def test_a_dot_prefixed_id_is_not_silently_accepted_as_a_hidden_dir():
    """``.hidden`` stays inside the root, so it is not an escape.

    It is still refused: a tenant id is a directory name, and an id starting
    with a dot produces a hidden directory (invisible to ``ls``, easy to
    mistake for metadata). ``.`` and ``..`` are already refused for the same
    reason — a path component must not be able to stand on its own. Every one
    of the 661 tenant ids in the live system starts with a letter or digit, so
    this constrains no real id.
    """
    with pytest.raises(ValueError):
        settings.get_tenant_db_path(".hidden")
