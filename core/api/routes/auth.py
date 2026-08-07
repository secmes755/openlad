"""
Authentication routes
Username/password login, exchange for API Key
"""
import logging

from fastapi import APIRouter, HTTPException
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
    Returns tenant_id + api_key after successful verification.
    """
    auth = get_auth_manager()
    user = auth.authenticate_by_password(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return {
        "tenant_id": user.tenant_id,
        "username": user.username,
        "role": user.role,
        "api_key": user.api_key,
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
async def logout():
    """Logout: revoke the current API key (the key used for this request
    becomes invalid immediately; the next login issues a fresh key)."""
    ctx = get_tenant_context()
    if ctx and ctx.user_id:
        auth = get_auth_manager()
        new_key = auth.regenerate_api_key(ctx.user_id)
        if new_key:
            logger.info(f"[AUTH] User logged out, API key revoked: {ctx.username} ({ctx.user_id})")
        else:
            logger.warning(f"[AUTH] Logout: failed to revoke API key for {ctx.username} ({ctx.user_id})")
    return {"success": True, "message": "Logged out"}
