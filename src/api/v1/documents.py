import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Query, UploadFile, status

from src.api.dependencies import StorageDep, TaskDispatcherDep, VectorDBDep
from src.core.exceptions import AppError, ServiceUnavailableError
from src.core.logging import request_id_ctx
from src.schemas.common import ErrorResponse
from src.schemas.documents import (
    DocumentDeleteResponse,
    DocumentTaskStatusResponse,
    DocumentUploadResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
TASK_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
DOCUMENT_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse, "description": "Bad Request"},
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": "Payload Too Large",
        },
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
            "model": ErrorResponse,
            "description": "Unsupported Media Type",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Service Unavailable",
        },
    },
    summary="Upload a document for processing",
)
async def upload_document(
    storage: StorageDep,
    task_dispatcher: TaskDispatcherDep,
    file: Annotated[UploadFile, File(description="PDF or TXT document to upload")],
    tenant_id: Annotated[
        str,
        Form(
            min_length=1,
            max_length=64,
            pattern=TENANT_ID_PATTERN,
            description="Tenant identifier for multi-tenant isolation",
        ),
    ],
) -> DocumentUploadResponse:
    """Dokümanı ortak depolama dizinine stream ederek kaydeder ve arka plan kuyruğuna atar."""
    stored = await storage.save(file=file, filename=file.filename)
    try:
        task_id = await task_dispatcher.enqueue_document(
            document_id=stored.document_id,
            file_path=stored.file_path,
            tenant_id=tenant_id,
            file_type=stored.file_type,
            request_id=request_id_ctx.get(),
        )
    except Exception as exc:
        await storage.delete(stored.file_path)
        logger.error(
            "Failed to enqueue document_id=%s for tenant_id=%s: %s",
            stored.document_id,
            tenant_id,
            type(exc).__name__,
        )
        raise ServiceUnavailableError("Document processing queue is currently unavailable") from exc

    return DocumentUploadResponse(
        document_id=stored.document_id,
        task_id=task_id,
        tenant_id=tenant_id,
        filename=stored.original_filename,
        file_type=stored.file_type,
        size_bytes=stored.size_bytes,
        status="queued",
    )


@router.get(
    "/status/{task_id}",
    response_model=DocumentTaskStatusResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Service Unavailable",
        },
    },
    summary="Get document processing task status",
)
async def get_document_task_status(
    task_dispatcher: TaskDispatcherDep,
    task_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=64,
            pattern=TASK_ID_PATTERN,
            description="Asynchronous task identifier returned by the upload endpoint",
        ),
    ],
) -> DocumentTaskStatusResponse:
    """Celery/Redis arka ucundan görevin güncel durumunu sorgular."""
    try:
        snapshot = await task_dispatcher.get_task_status(task_id=task_id)
    except Exception as exc:
        logger.error(
            "Failed to fetch status for task_id=%s: %s",
            task_id,
            type(exc).__name__,
        )
        raise ServiceUnavailableError(
            "Document processing status backend is currently unavailable"
        ) from exc

    return DocumentTaskStatusResponse(
        task_id=snapshot.task_id,
        status=snapshot.status,
        result=snapshot.result,
        error=snapshot.error,
    )


@router.delete(
    "/{document_id}",
    response_model=DocumentDeleteResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Service Unavailable",
        },
    },
    summary="Delete a document and its vectors for a tenant",
)
async def delete_document(
    vector_db: VectorDBDep,
    document_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=64,
            pattern=DOCUMENT_ID_PATTERN,
            description="Identifier of the document to delete",
        ),
    ],
    tenant_id: Annotated[
        str,
        Query(
            min_length=1,
            max_length=64,
            pattern=TENANT_ID_PATTERN,
            description="Tenant identifier for strict multi-tenant deletion isolation",
        ),
    ],
) -> DocumentDeleteResponse:
    """Belirtilen tenant_id altındaki dokümana ait tüm vektör kayıtlarını Qdrant'tan siler."""
    try:
        await vector_db.delete_by_document(tenant_id=tenant_id, document_id=document_id)
    except AppError:
        raise
    except Exception as exc:
        logger.error(
            "Failed to delete document_id=%s for tenant_id=%s: %s",
            document_id,
            tenant_id,
            type(exc).__name__,
        )
        raise ServiceUnavailableError("Vector database is currently unavailable") from exc

    logger.info("Deleted vectors for document_id=%s tenant_id=%s", document_id, tenant_id)
    return DocumentDeleteResponse(
        document_id=document_id,
        tenant_id=tenant_id,
        status="deleted",
    )
