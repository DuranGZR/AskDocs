from typing import Literal

from pydantic import BaseModel


class DocumentUploadResponse(BaseModel):
    """Doküman yükleme isteğinin standart yanıt şeması."""

    document_id: str
    task_id: str | None = None
    tenant_id: str
    filename: str
    file_type: str
    size_bytes: int
    status: Literal["stored", "queued"]
