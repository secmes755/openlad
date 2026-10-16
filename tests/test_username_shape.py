"""An account name that cannot form a tenant id must be refused up front.

An ordinary user gets an independent tenant whose id is ``u{username}``, and a
tenant id is used to build a filesystem path. A name containing anything outside
``[A-Za-z0-9_.-]`` — a CJK name, a space, a separator — therefore cannot become a
tenant, and the refusal has to happen at the entry point: validating later, inside
tenant creation, would leave a created tenant behind on failure.

The check is assigned its own status (400) instead of sharing the duplicate-name
409, so a caller can tell "this name is not usable" from "this name is taken".
"""
import pytest

from core.config import USERNAME_RULE, validate_username

# ── the rule itself ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["alice", "user01", "u_t3a", "a.b-c",
                                  "user_2024.01"])
def test_legal_names_pass(name):
    assert validate_username(name) == name


def test_the_length_budget_leaves_room_for_the_tenant_id_prefix():
    """The ceiling is the tenant id's 64 minus the ``u`` prefix — not 64.

    A 64-char name passes a naive shape check but ``u{name}`` is 65 and
    ``get_tenant_data_dir`` refuses it, so the account would be created while
    its tenant could never exist. The bound has to account for the prefix.
    """
    from core.config import _TENANT_ID_RE, USER_TENANT_PREFIX

    longest = "A" * (64 - len(USER_TENANT_PREFIX))
    assert validate_username(longest) == longest
    assert _TENANT_ID_RE.fullmatch(f"{USER_TENANT_PREFIX}{longest}")


@pytest.mark.parametrize("name", [
    "张三",              # CJK
    "user 01",           # space
    "user/01",           # separator
    "user\\01",          # backslash
    "../alice",          # traversal
    "..",                # leading dot — a path component on its own
    ".hidden",           # leading dot
    ".",                 # lone dot
    "-alice",            # leading hyphen
    "_alice",            # leading underscore
    "",                  # empty
    "A" * 64,            # legal name shape, but u{name} would be 65
    "a" * 65,            # too long even before the prefix
    "alice\n",           # control / newline
    "@alice",            # other symbols
])
def test_illegal_names_are_refused(name):
    with pytest.raises(ValueError):
        validate_username(name)


def test_non_string_names_are_refused():
    for bad in (None, 123, ["alice"], {"alice": 1}):
        with pytest.raises(ValueError):
            validate_username(bad)  # type: ignore[arg-type]


def test_the_error_message_states_the_rule():
    """The caller sees why, not just that it failed."""
    with pytest.raises(ValueError) as exc:
        validate_username("张三")
    assert USERNAME_RULE in str(exc.value)


# ── the invariant that ties the two rules together ───────────────────────

@pytest.mark.parametrize("name", ["alice", "user01", "u_t3a", "a.b-c",
                                  "A" * 63])
def test_every_legal_username_yields_a_legal_tenant_id(name):
    """``u{username}`` is how an ordinary user's tenant id is built.

    The 63-char case is the boundary that used to be missing: it is the longest
    name whose ``u{name}`` is still a legal tenant id. Without it the invariant
    was only ever exercised on short names, so the 64-char hole went unseen.
    """
    from core.config import settings, user_tenant_id

    tid = user_tenant_id(name)
    assert settings.get_tenant_data_dir(tid).name == tid


def test_an_over_long_name_is_refused_by_the_tenant_id_builder():
    """``user_tenant_id`` is the single definition of the convention."""
    from core.config import user_tenant_id

    with pytest.raises(ValueError):
        user_tenant_id("A" * 64)
    with pytest.raises(ValueError):
        user_tenant_id("张三")


# ── the account-creation path ────────────────────────────────────────────

def test_create_user_refuses_an_illegal_name_before_writing(tmp_path):
    """No row is written for a name that fails the shape check."""
    from core.db.system_db import SystemDB
    from core.tenant.auth import AuthManager

    db = SystemDB(db_path=tmp_path / "system.db")
    auth = AuthManager.__new__(AuthManager)
    auth.system_db = db

    with pytest.raises(ValueError):
        auth.create_user(tenant_id="u张三", username="张三", password="pw")

    assert db.find_users_by_username("张三") == []
    assert db.find_users_by_username("alice") == []


def test_create_user_still_accepts_a_legal_name(tmp_path):
    from core.db.system_db import SystemDB
    from core.tenant.auth import AuthManager

    db = SystemDB(db_path=tmp_path / "system.db")
    auth = AuthManager.__new__(AuthManager)
    auth.system_db = db

    user = auth.create_user(tenant_id="ualice", username="alice", password="pw")
    assert user.username == "alice"
    assert db.find_users_by_username("alice")


def test_create_user_refuses_a_tenant_id_it_could_not_provision(tmp_path):
    """A row whose tenant cannot exist must never be written.

    This is the shape of the original defect: the name passed its own check but
    ``u{name}`` was 65 characters, so tenant creation raised, the caller
    swallowed it, and the account was left pointing at a tenant that does not
    exist — created successfully and unable to authenticate, ever.
    """
    from core.db.system_db import SystemDB
    from core.tenant.auth import AuthManager

    db = SystemDB(db_path=tmp_path / "system.db")
    auth = AuthManager.__new__(AuthManager)
    auth.system_db = db

    with pytest.raises(ValueError):
        auth.create_user(
            tenant_id="u" + "A" * 64, username="alice", password="pw")

    assert db.find_users_by_username("alice") == []

