import logging
from pathlib import Path

from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="corerag.process_document")
def process_document_task(
    document_id: str,
    file_path: str,
    tenant_id: str,
    file_type: str,
) -> dict[str, object]:
    """Dokümanı arka planda işler (9. Günde PDF parsing ve Qdrant yazımı bağlanacak)."""
    target = Path(file_path)
    exists = target.is_file()
    size_bytes = target.stat().st_size if exists else 0

    logger.info(
        "Worker picked document_id=%s tenant_id=%s type=%s exists=%s size=%d",
        document_id,
        tenant_id,
        file_type,
        exists,
        size_bytes,
    )
    return {
        "document_id": document_id,
        "tenant_id": tenant_id,
        "file_type": file_type,
        "file_exists": exists,
        "size_bytes": size_bytes,
        "status": "processed_stub",
    }
