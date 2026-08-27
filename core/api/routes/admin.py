"""
Admin backend routes
Tenant management, user management
Built-in admin tenant (admin) and admin user, auto-initialized on startup.
When admin creates ordinary users, each user is automatically assigned an independent tenant (physical isolation).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services import model_config
from ...services.resource_capacity import get_capacity_manager
from ...tenant.auth import get_auth_manager
from ...tenant.tenant_manager import get_tenant_manager

router = APIRouter()


class CreateTenantRequest(BaseModel):
    name: str
    description: str | None = ""
    industry_packages: list[str] | None = []
    storage_quota_mb: int | None = None
    tenant_id: str | None = None  # Supports custom tenant ID, e.g. u001, zhangsan


class CreateUserRequest(BaseModel):
    tenant_id: str | None = "admin"  # When admin creates users, ordinary users get independent tenants
    username: str | None = None
    password: str | None = None
    email: str | None = None
    role: str | None = "user"
    auto_username: bool | None = False
    count: int | None = 1
    custom_tenant_id: str | None = None  # Specify custom tenant ID for ordinary users
    api_key_ttl_days: int | None = None  # API key lifetime: None=config default(90d), <=0=never expires


def _require_admin():
    """Strict admin privilege check"""
    from ...tenant.context import get_tenant_context
    ctx = get_tenant_context()
    if not ctx or ctx.user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")


@router.post("/tenants")
async def create_tenant(req: CreateTenantRequest):
    """Create new tenant (supports custom tenant_id)"""
    _require_admin()
    mgr = get_tenant_manager()
    try:
        info = mgr.create_tenant(
            name=req.name,
            description=req.description,
            industry_packages=req.industry_packages,
            storage_quota_mb=req.storage_quota_mb,
            tenant_id=req.tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "tenant_id": info.id,
        "name": info.name,
        "status": info.status,
        "created_at": info.created_at.isoformat()
    }


@router.get("/tenants")
async def list_tenants():
    """List all tenants"""
    _require_admin()
    mgr = get_tenant_manager()
    tenants = mgr.list_tenants()
    return {
        "tenants": [
            {
                "id": t.id,
                "name": t.name,
                "status": t.status,
                "storage_quota_mb": t.storage_quota_mb,
                "created_at": t.created_at.isoformat()
            }
            for t in tenants
        ]
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str):
    """Get tenant details"""
    _require_admin()
    mgr = get_tenant_manager()
    tenant = mgr.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    usage = mgr.get_tenant_storage_usage(tenant_id)
    return {
        "id": tenant.id,
        "name": tenant.name,
        "description": tenant.description,
        "status": tenant.status,
        "industry_packages": tenant.industry_packages,
        "storage_quota_mb": tenant.storage_quota_mb,
        "storage_usage": usage,
        "created_at": tenant.created_at.isoformat()
    }


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str, hard: bool = False):
    """Delete tenant (admin only)"""
    _require_admin()
    mgr = get_tenant_manager()
    success = mgr.delete_tenant(tenant_id, hard_delete=hard)
    return {"success": success}


@router.post("/users")
async def create_user(req: CreateUserRequest):
    """Create user

    - Create admin: must be under admin tenant, requires current user to be admin
    - Create ordinary user: auto-creates independent tenant (physical isolation), tenant_id format: u{username} or custom_tenant_id
    """
    _require_admin()
    auth = get_auth_manager()
    tenant_mgr = get_tenant_manager()

    # Capacity check — calculate max tenants based on hardware resources
    capacity_mgr = get_capacity_manager()
    all_tenants = tenant_mgr.list_tenants()
    can_create, reason = capacity_mgr.can_create_tenant(len(all_tenants))
    if not can_create:
        raise HTTPException(status_code=503, detail=reason)

    count = max(1, min(req.count or 1, 100))

    # Determine starting number (e.g. if users user_01, user_02 exist, start from 03)
    existing = auth.list_users("admin")
    existing_nums = set()
    for u in existing:
        if u.username.startswith("user"):
            try:
                existing_nums.add(int(u.username.replace("user", "")))
            except ValueError:
                pass

    users = []
    for i in range(count):
        if req.auto_username or not req.username:
            next_num = 1
            while next_num in existing_nums:
                next_num += 1
            existing_nums.add(next_num)
            username = f"user{next_num:02d}"
        else:
            username = req.username
            if count > 1:
                username = f"{req.username}{i + 1:02d}"

        # Auto-generate random password (if not provided)
        password = req.password
        if not password:
            import secrets
            password = secrets.token_urlsafe(8)

        # Determine target tenant
        target_role = req.role or "user"
        if target_role == "admin":
            # admin can only be created under admin tenant
            target_tenant_id = "admin"
        else:
            # Ordinary user gets independent tenant
            if req.custom_tenant_id:
                target_tenant_id = req.custom_tenant_id
            else:
                target_tenant_id = f"u{username}"

            # Ensure tenant exists
            existing_tenant = tenant_mgr.get_tenant(target_tenant_id)
            if not existing_tenant:
                try:
                    tenant_mgr.create_tenant(
                        name=f"{username}'s personal space",
                        description=f"Independent document space for user {username}",
                        tenant_id=target_tenant_id,
                        storage_quota_mb=5120
                    )
                except ValueError:
                    # Tenant ID already exists (rare), skip creation
                    pass
            elif existing_tenant.status != "active":
                # Tenant record exists but is unusable (soft-deleted): reactivate
                # it so the recreated user's API key works immediately instead of
                # every request returning 403 "Tenant not found or inactive".
                tenant_mgr.reactivate_tenant(target_tenant_id)

        try:
            user = auth.create_user(
                tenant_id=target_tenant_id,
                username=username,
                password=password,
                email=req.email,
                role=target_role,
                api_key_ttl_days=req.api_key_ttl_days
            )
        except ValueError as e:
            # Duplicate username in tenant (application-level guard)
            raise HTTPException(status_code=409, detail=str(e))
        users.append({
            "user_id": user.id,
            "username": user.username,
            "password": password,  # Only returned on creation, for admin distribution
            "tenant_id": user.tenant_id,
            "role": user.role,
            "api_key": user.api_key,
            "api_key_expires_at": user.api_key_expires_at.isoformat() if user.api_key_expires_at else None,
            "created_at": user.created_at.isoformat()
        })

    if len(users) == 1:
        return users[0]
    return {"users": users, "count": len(users)}


@router.get("/users")
async def list_all_users():
    """List all users (admin only)"""
    _require_admin()
    auth = get_auth_manager()
    users = auth.list_all_users()
    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "tenant_id": u.tenant_id,
                "email": u.email,
                "role": u.role,
                "api_key": u.api_key[:16] + "..." if u.api_key else None,
                "api_key_expires_at": u.api_key_expires_at.isoformat() if u.api_key_expires_at else None,
                "created_at": u.created_at.isoformat()
            }
            for u in users
        ]
    }


@router.delete("/users/{user_id}")
async def delete_user(user_id: str):
    """Delete user (admin only, cannot delete self)"""
    _require_admin()
    from ...tenant.context import get_tenant_context
    ctx = get_tenant_context()
    if ctx and ctx.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete currently logged-in user")

    auth = get_auth_manager()
    user = auth.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    success = auth.delete_user(user_id)
    return {"success": success, "deleted_user": user.username}


class UpdateUserRequest(BaseModel):
    role: str | None = None
    email: str | None = None
    password: str | None = None


@router.patch("/users/{user_id}")
async def update_user(user_id: str, req: UpdateUserRequest):
    """Update user info (admin only)"""
    _require_admin()
    auth = get_auth_manager()
    user = auth.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    updates = {}
    if req.role is not None:
        updates["role"] = req.role
    if req.email is not None:
        updates["email"] = req.email
    if req.password is not None:
        updates["password"] = req.password

    if updates:
        success = auth.update_user(user_id, **updates)
        return {"success": success}
    return {"success": True, "message": "No updates"}


class RegenerateKeyRequest(BaseModel):
    api_key_ttl_days: int | None = None  # None=config default(90d), <=0=never expires


@router.post("/users/{user_id}/regenerate-key")
async def regenerate_api_key(user_id: str, req: RegenerateKeyRequest | None = None):
    """Rotate a user's API key and reset its expiry (admin only).

    Use this when a key expires or needs rotation. Returns the new key.
    """
    _require_admin()
    auth = get_auth_manager()
    user = auth.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    ttl = req.api_key_ttl_days if req else None
    new_key = auth.regenerate_api_key(user_id, ttl)
    if not new_key:
        raise HTTPException(status_code=500, detail="Failed to regenerate API key")
    updated = auth.get_user(user_id)
    return {
        "user_id": user_id,
        "username": user.username,
        "api_key": new_key,
        "api_key_expires_at": updated.api_key_expires_at.isoformat() if updated and updated.api_key_expires_at else None,
    }


@router.get("/tenants/{tenant_id}/users")
async def list_tenant_users(tenant_id: str):
    """List users under a tenant"""
    _require_admin()
    auth = get_auth_manager()
    users = auth.list_users(tenant_id)
    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "tenant_id": u.tenant_id,
                "email": u.email,
                "role": u.role,
                "api_key_expires_at": u.api_key_expires_at.isoformat() if u.api_key_expires_at else None,
                "created_at": u.created_at.isoformat()
            }
            for u in users
        ]
    }


# =========================================================================
# Model backend configuration (runtime, local & cloud unified)
#
# Any OpenAI-compatible endpoint = (url, api_key, model). Local backends
# ignore the key; an unset key resolves to a placeholder ("123") so one
# code path serves both. Keys are write-only over this API — reads come
# back masked.
# =========================================================================


class ModelConfigRequest(BaseModel):
    """Omitted / null fields are left untouched. Empty string clears the
    stored override (falls back to env/default). API keys: send the secret
    to set or rotate it; omit to keep; empty string reverts to default."""
    llm_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    emb_url: str | None = None
    emb_api_key: str | None = None
    emb_model: str | None = None


class ModelTestRequest(BaseModel):
    target: str = "llm"                    # "llm" | "emb"
    url: str | None = None                 # optional unsaved values to test first
    api_key: str | None = None             # never persisted; test-only
    model: str | None = None               # informational echo in response


@router.get("/models/config")
async def get_model_config():
    """Current effective model backends (api keys masked)"""
    _require_admin()
    from ...models.client import get_model_client  # ensure one exists
    get_model_client()
    return model_config.public_view(model_config.get_model_settings())


@router.put("/models/config")
async def put_model_config(req: ModelConfigRequest):
    """Save and hot-apply model backend config (no restart needed)"""
    _require_admin()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        view = model_config.update_model_settings(updates)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save: {e}")
    return {"success": True, "config": view}


@router.post("/models/test")
async def test_model_endpoint(req: ModelTestRequest):
    """Probe an endpoint before/without saving. Falls back to the current
    effective URL/key for the chosen target when url/key omitted."""
    _require_admin()
    resolved = model_config.get_model_settings()
    if req.target == "llm":
        url = req.url if req.url is not None else resolved["llm_url"]
        key = req.api_key if req.api_key is not None else resolved["llm_api_key"]
    elif req.target == "emb":
        url = req.url if req.url is not None else resolved["emb_url"]
        key = req.api_key if req.api_key is not None else resolved["emb_api_key"]
    else:
        raise HTTPException(status_code=400, detail="target must be 'llm' or 'emb'")
    result = model_config.probe_endpoint(url, api_key=key)
    result["target"] = req.target
    result["url_tested"] = url.rstrip("/")
    if req.model:
        result["model_requested"] = req.model
    return result
