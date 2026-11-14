from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status

from src.api.dependencies import StorageDep
from src.schemas.common import ErrorResponse
from src.schemas.documents import DocumentUploadResponse

router = APIRouter(prefix="/documents", tags=["documents"])

TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


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
    },
    summary="Upload a document for processing",
)
async def upload_document(
    storage: StorageDep,
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
    """Dokümanı ortak depolama dizinine stream ederek kaydeder."""
    stored = await storage.save(file=file, filename=file.filename)
    return DocumentUploadResponse(
        document_id=stored.document_id,
        task_id=None,
        tenant_id=tenant_id,
        filename=stored.original_filename,
        file_type=stored.file_type,
        size_bytes=stored.size_bytes,
        status="stored",
    )
