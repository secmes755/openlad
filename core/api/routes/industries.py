"""
Industry Package Routes
"""
from fastapi import APIRouter

from ...plugins import get_plugin_registry

router = APIRouter()


@router.get("/industries")
async def list_industries():
    """List all available industry packages"""
    registry = get_plugin_registry()
    plugins = registry.list_plugins()
    # Convert to array format, compatible with frontend
    return {
        "industries": [
            {"id": pid, **info}
            for pid, info in plugins.items()
        ]
    }
