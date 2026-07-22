"""
Authentication and Authorization Management
Supports API Key + username/password authentication
"""
import logging
import secrets
import bcrypt
from datetime import datetime
from typing import Optional, List, Dict

from .models import UserInfo

logger = logging.getLogger(__name__)


class AuthManager:
    """Authentication Manager"""

    def __init__(self):
        from ..db.system_db import get_system_db
        self.system_db = get_system_db()

    @staticmethod
    def _hash_password(password: str) -> str:
        """Password hashing (bcrypt + auto-salt)"""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        """Verify password"""
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _generate_api_key() -> str:
        """Generate API Key"""
        return "ak_" + secrets.token_urlsafe(32)

    def create_user(self, tenant_id: str, username: str, password: str = None,
                    email: str = None, role: str = "user") -> UserInfo:
        """Create user"""
        user_id = secrets.token_hex(16)
        api_key = self._generate_api_key()
        password_hash = self._hash_password(password) if password else None

        user = UserInfo(
            id=user_id,
            tenant_id=tenant_id,
            username=username,
            email=email,
            role=role,
            api_key=api_key,
            created_at=datetime.now(),
        )
        self.system_db.create_user(user, password_hash)
        logger.info(f"[AUTH] Created user: {username} in tenant {tenant_id}")
        return user

    def authenticate_by_password(self, username: str, password: str,
                                   tenant_id: str = None) -> Optional[UserInfo]:
        """Username/password authentication, supports exact tenant matching"""
        users = self.system_db.find_users_by_username(username, tenant_id)
        for user in users:
            # bcrypt: verify one by one (each hash is unique, cannot compare directly in SQL)
            if user.password_hash and self._verify_password(password, user.password_hash):
                return user
        return None

    def authenticate_by_api_key(self, api_key: str) -> Optional[UserInfo]:
        """API Key authentication"""
        return self.system_db.get_user_by_api_key(api_key)

    def get_user(self, user_id: str) -> Optional[UserInfo]:
        """Get user info"""
        return self.system_db.get_user(user_id)

    def list_users(self, tenant_id: str) -> List[UserInfo]:
        """List all users under a tenant"""
        return self.system_db.list_users(tenant_id)

    def list_all_users(self) -> List[UserInfo]:
        """List all users (across all tenants)"""
        return self.system_db.list_all_users()

    def delete_user(self, user_id: str) -> bool:
        """Delete user"""
        return self.system_db.delete_user(user_id)

    def update_user(self, user_id: str, **kwargs) -> bool:
        """Update user info"""
        if "password" in kwargs:
            kwargs["password_hash"] = self._hash_password(kwargs.pop("password"))
        return self.system_db.update_user(user_id, **kwargs)

    def regenerate_api_key(self, user_id: str) -> Optional[str]:
        """Regenerate API Key"""
        new_key = self._generate_api_key()
        if self.system_db.update_user_api_key(user_id, new_key):
            return new_key
        return None

    def check_permission(self, user: UserInfo, resource_type: str,
                         resource_id: str, action: str) -> bool:
        """Check user permission"""
        if user.role == "admin":
            return True
        # Regular user: deny by default, whitelist rules added as needed at call sites
        logger.warning(
            f"Permission denied: user={user.username}({user.role}) "
            f"resource={resource_type}/{resource_id} action={action}"
        )
        return False


# Singleton
_auth_manager: Optional[AuthManager] = None


def get_auth_manager() -> AuthManager:
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = AuthManager()
    return _auth_manager
