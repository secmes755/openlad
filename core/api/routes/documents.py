"""
Document Routes
Upload, List, Detail, Delete
"""
import asyncio
import logging
import os
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

logger = logging.getLogger(__name__)

from pydantic import BaseModel

from ...db.tenant_db import get_tenant_metadata_db
from ...ingestion.parser import DocumentParser
from ...tenant.context import get_tenant_context

router = APIRouter()

# Upload limits
MAX_FILE_SIZE_MB = int(os.environ.get("OPENLAD_MAX_FILE_SIZE_MB", "100"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
SUPPORTED_EXTENSIONS = DocumentParser.SUPPORTED_EXTENSIONS

# Background task status storage — persisted in SQLite (upload_tasks table)
# In-memory dict removed in v2: tasks survive server restarts via system_db

def _get_system_db():
    from ...db.system_db import get_system_db
    return get_system_db()


class DocumentListResponse(BaseModel):
    id: str
    filename: str
    title: str | None
    status: str
    category_level1: str | None
    industry_package_id: str | None
    created_at: str


def _create_task(doc_id: str, filename: str, tenant_id: str = "") -> str:
    """Create upload task record in SQLite"""
    task_id = str(uuid.uuid4())
    db = _get_system_db()
    db.create_upload_task(
        task_id=task_id,
        doc_id=doc_id,
        tenant_id=tenant_id,
        filename=filename
    )
    return task_id


def _update_task(task_id: str, **kwargs):
    """Update task status in SQLite"""
    db = _get_system_db()
    db.update_upload_task(task_id, **kwargs)


def _cleanup_old_tasks(max_age_seconds: int = 3600):
    """Clean up expired tasks from SQLite"""
    db = _get_system_db()
    db.cleanup_upload_tasks(max_age_hours=max(1, max_age_seconds // 3600))


def _process_document_async_sync(task_id: str, tenant_id: str, file_path: str,
                                  industry: str | None, auto_detect: bool, builder,
                                  title: str | None = None):
    """Sync wrapper for document processing — runs directly in a thread pool.

    Uses builder.ingest_document() entry point with built-in MD5 dedup protection.
    """
    def _progress_callback(p, msg):
        _update_task(task_id, status="processing", progress=p, message=msg)

    _update_task(task_id, status="processing", progress=5, message="Starting document processing")
    try:
        result = builder.ingest_document(
            file_path=file_path,
            tenant_id=tenant_id,
            industry_hint=industry if not auto_detect else None,
            auto_confirm=True,
            progress_callback=_progress_callback,
            title=title
        )
        doc_id = result.get("doc_id", "")
        status = result.get("status", "unknown")

        if status == "already_imported":
            from pathlib import Path
            try:
                Path(file_path).unlink(missing_ok=True)
            except Exception:
                pass
            _update_task(
                task_id,
                status="completed",
                progress=100,
                message=result.get("message", "Document already exists, skipping import"),
                result={"status": "already_imported", "existing_doc_id": doc_id}
            )
        else:
            _update_task(
                task_id,
                status="completed",
                progress=100,
                message="Document processing complete",
                result={"status": result.get("status", "completed"), "doc_id": doc_id}
            )

        # Record audit log
        try:
            from ...db.tenant_db import get_tenant_metadata_db
            from ...tenant.context import get_tenant_context
            ctx = get_tenant_context()
            db = get_tenant_metadata_db(tenant_id)
            db.log_audit(
                action="document_upload",
                resource_type="document",
                resource_id=doc_id,
                user_id=ctx.user_id if ctx else "",
                tenant_id=tenant_id,
                details={"file_path": file_path, "status": status}
            )
        except Exception as e:
            logger.warning(f"Failed to record audit log: {e}")

        logger.info(f"[UPLOAD_TASK] Document processing complete: task={task_id}, doc={doc_id}, status={status}")
    except Exception as e:
        logger.error(f"[UPLOAD_TASK] Document processing failed: task={task_id}, error={e}", exc_info=True)
        _update_task(
            task_id,
            status="failed",
            progress=0,
            message=f"Processing failed: {str(e)}",
            error=str(e)
        )


@router.post("/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    industry: str | None = Form(None),
    auto_detect: bool = Form(True),
    title: str | None = Form(None)
):
    """Upload document (async background processing)

    Returns task_id after successful upload. Query processing progress via /documents/upload-progress/{task_id}.
    title: optional explicit document title (highest priority in title derivation;
    when omitted the title is auto-derived from the L1 summary + filename).
    """
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    # File type validation
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file format: {suffix}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    # File size validation (streamed to temp file, avoid memory accumulation)
    import tempfile
    size = 0
    tmp_fd, tmp_path = tempfile.mkstemp(dir=ctx.get_documents_dir(), prefix="upload_")
    try:
        with os.fdopen(tmp_fd, 'wb') as tmp_f:
            chunk = await file.read(8192)
            while chunk:
                size += len(chunk)
                tmp_f.write(chunk)
                if size > MAX_FILE_SIZE_BYTES:
                    tmp_f.close()
                    os.unlink(tmp_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File size exceeds limit: {MAX_FILE_SIZE_MB}MB"
                    )
                chunk = await file.read(8192)
    except Exception:
        os.unlink(tmp_path)
        raise

    # Reject empty files
    if size == 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail="Cannot upload empty file")

    # Check storage quota
    from ...tenant.tenant_manager import get_tenant_manager
    tenant_mgr = get_tenant_manager()
    usage = tenant_mgr.get_tenant_storage_usage(ctx.tenant_id)
    tenant = tenant_mgr.get_tenant(ctx.tenant_id)
    quota_mb = tenant.storage_quota_mb if tenant else 10240
    usage_mb = usage.get("total_mb", 0)
    if usage_mb >= quota_mb:
        os.unlink(tmp_path)
        raise HTTPException(status_code=413, detail=f"Storage quota full: {usage_mb:.1f}MB / {quota_mb}MB")

    # Save uploaded file (rename temp file to final path)
    doc_id = str(uuid.uuid4())
    # FIX: Clean up existing doc_id/UUID prefix in filename to avoid double stacking
    import re
    clean_filename = re.sub(
        r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}_',
        '',
        file.filename or ""
    )
    clean_filename = clean_filename or (file.filename or "upload.pdf")
    clean_filename = os.path.basename(clean_filename)
    upload_path = ctx.get_documents_dir() / f"{doc_id}_{clean_filename}"
    os.rename(tmp_path, upload_path)
    # Check again (after upload)
    usage_after = tenant_mgr.get_tenant_storage_usage(ctx.tenant_id)
    if usage_after.get("total_mb", 0) > quota_mb:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=f"Exceeds storage quota after upload: {quota_mb}MB")

    # Database recording is handled uniformly by builder.ingest_document() (includes MD5 dedup)
    # No longer calling db.save_document() here, to avoid creating orphan records without hash

    # Create background task
    builder = getattr(request.app.state, "builder", None)
    if builder is None:
        raise HTTPException(status_code=503, detail="Document builder not initialized")

    task_id = _create_task(doc_id, file.filename, tenant_id=ctx.tenant_id)

    # Use a dedicated thread pool executor for document processing to avoid
    # competing with the default asyncio executor (which is limited to N threads).
    # The builder internally creates its own thread pools for page processing;
    # using a separate outer pool prevents deadlocks when all default threads
    # are occupied by nested ThreadPoolExecutor workers.
    import concurrent.futures

    async def _background_process():
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ol_upload_") as pool:
                await asyncio.get_event_loop().run_in_executor(
                    pool,
                    lambda: _process_document_async_sync(
                        task_id, ctx.tenant_id, str(upload_path),
                        industry, auto_detect, builder, title=title
                    )
                )
        except Exception as e:
            logger.error(f"[UPLOAD] Background task failed: {e}", exc_info=True)
            _update_task(task_id, status="failed", progress=0,
                        message=f"Background processing error: {str(e)}", error=str(e))

    asyncio.create_task(_background_process())

    # Periodically clean up old tasks
    _cleanup_old_tasks()

    # Audit log moved into _process_document_async (recorded after obtaining real doc_id)
    return {
        "doc_id": doc_id,
        "task_id": task_id,
        "filename": file.filename,
        "status": "pending_processing",
        "message": "Document uploaded, processing in background",
        "check_progress_url": f"/api/v1/documents/upload-progress/{task_id}"
    }


@router.get("/documents/upload-progress/{task_id}")
async def get_upload_progress(task_id: str):
    """Query upload task processing progress"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    db = _get_system_db()
    task = db.get_upload_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or expired")

    # Prevent cross-tenant access to upload tasks
    if task.get("tenant_id") and task["tenant_id"] != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="No permission to access this task")
    return {
        "task_id": task["task_id"],
        "doc_id": task["doc_id"],
        "filename": task["filename"],
        "status": task["status"],
        "progress": task["progress"],
        "message": task["message"],
        "result": task["result"],
        "error": task["error"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


@router.get("/documents")
async def list_documents(status: str | None = None,
                         industry: str | None = None,
                         skip: int = 0, limit: int = 100):
    """List documents"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    db = get_tenant_metadata_db(ctx.tenant_id)
    total = db.count_documents(status=status, industry_package_id=industry)
    docs = db.list_documents(status=status, industry_package_id=industry, skip=skip, limit=limit)
    return {
        "total": total,
        "documents": docs
    }


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str):
    """Get document details"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    db = get_tenant_metadata_db(ctx.tenant_id)
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Delete document"""
    ctx = get_tenant_context()
    if not ctx:
        raise HTTPException(status_code=400, detail="Tenant context required")

    db = get_tenant_metadata_db(ctx.tenant_id)
    success = db.delete_document(doc_id)
    vector_cleaned = False
    # Also delete vectors
    if success:
        from ...db.tenant_db import get_tenant_vector_db
        vector_db = get_tenant_vector_db(ctx.tenant_id)
        vector_cleaned = vector_db.delete_doc_vectors(doc_id)
        if not vector_cleaned:
            logger.warning(f"[DELETE] Document metadata deleted, but vector cleanup failed: {doc_id}")
        # Record audit log
        try:
            db.log_audit(
                action="document_delete",
                resource_type="document",
                resource_id=doc_id,
                user_id=ctx.user_id,
                tenant_id=ctx.tenant_id
            )
        except Exception as e:
            logger.warning(f"Failed to record audit log: {e}")
    return {"success": success, "vector_cleaned": vector_cleaned}
