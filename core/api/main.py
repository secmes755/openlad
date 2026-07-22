"""
OpenLAD API Service Entry Point
"""
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Load .env file before any other imports that might read env vars
from dotenv import load_dotenv
load_dotenv()

from ..config import settings
from ..plugins import get_plugin_registry
from ..db.system_db import get_system_db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("OpenLAD service starting...")
    try:
        # Initialize system database
        system_db = get_system_db()
        system_db.init_schema()
        logger.info("[LIFESPAN] System database initialized")

        # Load industry packages
        registry = get_plugin_registry()
        plugins = registry.list_plugins()
        logger.info(f"[LIFESPAN] Loaded {len(plugins)} industry packages: {list(plugins.keys())}")

        # Auto-initialize built-in admin tenant + admin user
        try:
            from ..tenant.tenant_manager import get_tenant_manager
            from ..tenant.auth import get_auth_manager
            tenant_mgr = get_tenant_manager()
            auth_mgr = get_auth_manager()

            admin_tenant = tenant_mgr.get_tenant("admin")
            if not admin_tenant:
                tenant_mgr.create_tenant(
                    name="System Administrator",
                    description="Built-in admin tenant",
                    tenant_id="admin",
                    storage_quota_mb=10240
                )
                logger.info("[LIFESPAN] Built-in admin tenant created")

            # Check if admin user exists
            admin_users = auth_mgr.list_users("admin")
            if not any(u.username == "admin" for u in admin_users):
                # FIX: No default password — require explicit OPENLAD_ADMIN_PASSWORD env var
                admin_password = os.environ.get("OPENLAD_ADMIN_PASSWORD")
                if not admin_password:
                    logger.error("[LIFESPAN] OPENLAD_ADMIN_PASSWORD environment variable is not set. "
                                 "Admin user cannot be created. Please set it before first startup.")
                    raise RuntimeError("OPENLAD_ADMIN_PASSWORD is required for initial admin creation")
                user = auth_mgr.create_user(
                    tenant_id="admin",
                    username="admin",
                    password=admin_password,
                    role="admin"
                )
                logger.info(f"[LIFESPAN] Built-in admin user created")
            else:
                logger.info("[LIFESPAN] Built-in admin user already exists")
        except Exception as e:
            logger.error(f"[LIFESPAN] admin initialization failed: {e}", exc_info=True)

        # Initialize core engine (each component initialized independently, no blocking)
        try:
            from ..ingestion.builder import DocumentIndexBuilder
            app.state.builder = DocumentIndexBuilder()
            logger.info("[LIFESPAN] Document builder initialization complete")
        except Exception as e:
            logger.error(f"[LIFESPAN] Document builder initialization failed: {e}", exc_info=True)

        try:
            from ..retrieval.engine import QueryEngine
            app.state.query_engine = QueryEngine()
            logger.info("[LIFESPAN] Retrieval engine initialization complete")
        except Exception as e:
            logger.error(f"[LIFESPAN] Retrieval engine initialization failed: {e}", exc_info=True)

        # Check external model service reachability (do not start processes)
        try:
            from ..services.manager import get_service_manager
            mgr = get_service_manager()
            statuses = mgr.get_status()
            for svc_key, svc in statuses.items():
                if svc.status == "ok":
                    logger.info(f"[LIFESPAN] {svc.name} reachable ({svc.url})")
                else:
                    logger.warning(
                        f"[LIFESPAN] {svc.name} unreachable ({svc.url}): {svc.last_error}. "
                        f"Please verify LLM/Embedding services are running, or set correct OPENLAD_LLM_URL / OPENLAD_EMB_URL"
                    )
        except Exception as e:
            logger.error(f"[LIFESPAN] Service reachability check failed: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Initialization failed: {e}", exc_info=True)
    yield
    logger.info("OpenLAD service shutting down...")


app = FastAPI(
    title="OpenLAD - Intelligent Document Analysis System",
    description="Local document intelligent Q&A system with multi-tenant support and industry plugins",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=(settings.CORS_ORIGINS != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register multi-tenant middleware
from .middleware.tenant import TenantMiddleware
app.add_middleware(TenantMiddleware)

# Register rate limiting middleware
from .middleware.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)

# Register global exception handlers
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse


@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status_code": exc.status_code}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Uncaught exception: {exc}", exc_info=True)
    debug_mode = os.environ.get("OPENLAD_DEBUG", "").lower() in ("true", "1", "yes")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "status_code": 500, "detail": str(exc) if debug_mode else None}
    )


# Register routes
from .routes import health, admin, documents, query, skill, industries, auth, diagnostic

app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1/admin")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(query.router, prefix="/api/v1")
app.include_router(skill.router, prefix="/api/v1/skill")
app.include_router(industries.router, prefix="/api/v1")
app.include_router(diagnostic.router, prefix="/api/v1")

# Static files
static_dir = str(settings.STATIC_DIR)
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Static file directory mounted: {static_dir}")


@app.get("/")
async def root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "name": "OpenLAD",
        "version": "1.0.0",
        "description": "OpenLAD Intelligent Document Analysis System"
    }


@app.get("/admin")
async def admin_page():
    admin_path = os.path.join(static_dir, "admin.html")
    if os.path.exists(admin_path):
        return FileResponse(admin_path)
    raise HTTPException(status_code=404, detail="Admin page not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT)
