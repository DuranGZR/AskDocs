from typing import Any, Literal

from pydantic import BaseModel

from src.services.base import TaskState


class DocumentUploadResponse(BaseModel):
    """Doküman yükleme isteğinin standart yanıt şeması."""

    document_id: str
    task_id: str
    tenant_id: str
    filename: str
    file_type: str
    size_bytes: int
    status: Literal["queued"] = "queued"


class DocumentTaskStatusResponse(BaseModel):
    """Arka plan doküman işleme görevinin durum sorgulama yanıt şeması."""

    task_id: str
    status: TaskState
    result: dict[str, Any] | None = None
    error: str | None = None
