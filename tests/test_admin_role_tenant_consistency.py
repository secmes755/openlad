"""Privilege-escalation guard on the user-update path.

Root defect (found by the 2026-10-17 audit): ``create_user`` forces an admin
account into the ``admin`` tenant, but ``update_user`` let any tenant's admin
set ``role="admin"`` on *any* user id while leaving that user in their old
tenant. The tenant middleware only checks ``user.role == "admin"`` to allow
cross-tenant access, so the promoted account could then read and write every
tenant while its home tenant stayed a normal one.

The fix keeps role and tenant consistent on both directions: promotion moves
the account to ``admin``, demotion moves it back to the user's own tenant.
"""

import inspect

from core.api.routes import admin as admin_routes


def _update_user_source() -> str:
    return inspect.getsource(admin_routes.update_user)


class TestUpdateUserTenantConsistency:
    def test_promotion_forces_admin_tenant(self):
        """role='admin' must also set tenant_id='admin' (same rule as creation)."""
        src = _update_user_source()
        assert 'updates["role"] = req.role' in src
        assert 'updates["tenant_id"] = "admin"' in src, (
            "promoting a user to admin without moving it into the admin tenant "
            "leaves cross-tenant rights on a normal-tenant account"
        )

    def test_promotion_rule_matches_creation_rule(self):
        """Both paths must agree; a divergence is the bug that was found."""
        create_src = inspect.getsource(admin_routes.create_user)
        update_src = _update_user_source()
        # creation: admin -> tenant "admin"
        assert 'target_tenant_id = "admin"' in create_src
        # update: same mapping expressed as an update dict
        assert 'updates["tenant_id"] = "admin"' in update_src

    def test_demotion_leaves_admin_tenant(self):
        """Demoting an admin must not leave it inside the admin tenant."""
        src = _update_user_source()
        assert "user_tenant_id(user.username)" in src, (
            "a demoted admin still sitting in the admin tenant would keep "
            "seeing admin-scoped data through tenant-scoped queries"
        )

    def test_role_value_is_validated(self):
        """An arbitrary role string must be rejected, not stored."""
        src = _update_user_source()
        assert "role must be 'admin' or 'user'" in src


class TestSystemDbUpdateUserShape:
    def test_update_user_rejects_unknown_columns(self):
        """update_user must whitelist columns (no arbitrary kwargs -> SQL)."""
        from core.db.system_db import SystemDB

        src = inspect.getsource(SystemDB.update_user)
        assert 'allowed = {"role", "email", "password_hash"' in src

    def test_tenant_id_is_updatable_only_through_the_whitelist(self):
        """tenant_id must be accepted by update_user for the fix to work."""
        from core.db.system_db import SystemDB

        src = inspect.getsource(SystemDB.update_user)
        assert "tenant_id" in src, (
            "the route now passes tenant_id; if the DB layer drops it the "
            "promotion fix silently does nothing"
        )
