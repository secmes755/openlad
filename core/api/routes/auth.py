"""
Authentication routes
Username/password login, exchange for API Key
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...tenant.auth import get_auth_manager
from ...tenant.context import get_tenant_context

router = APIRouter()
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/login")
async def login(req: LoginRequest):
    """Username/password login

    Only username + password required, system auto-infers the associated tenant.
    Returns tenant_id + a fresh session api_key after successful verification.
    Each login issues its own session key; logout revokes only that key.
    """
    auth = get_auth_manager()
    user = auth.authenticate_by_password(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    session_key = auth.create_session(user.id)
    return {
        "tenant_id": user.tenant_id,
        "username": user.username,
        "role": user.role,
        # session key on success; fall back to the account key if session
        # creation failed for any reason
        "api_key": session_key or user.api_key,
    }


@router.get("/me")
async def me():
    """Get current logged-in user info (authenticated via API Key)"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "username": ctx.username,
        "role": ctx.user_role,
    }


@router.post("/logout")
async def logout(request: Request):
    """Logout: revoke the session key used for this request.

    Only the current device's login session dies — other devices that logged
    in with the same username keep working. The account's primary key is not
    affected (admins can rotate it to force an account-wide revocation).
    """
    auth = get_auth_manager()
    api_key = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if api_key:
        revoked = auth.revoke_session_by_key(api_key)
        if revoked:
            logger.info("[AUTH] Session revoked via logout")
        # No match: the request used the primary account key — leave it alone.
    return {"success": True, "message": "Logged out"}
