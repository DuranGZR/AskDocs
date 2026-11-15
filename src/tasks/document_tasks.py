import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.core.logging import request_id_ctx
from src.services.base import DocumentTaskDispatcher, TaskStatusSnapshot
from src.services.pdf_parser import TransientProcessingError, parse_and_chunk_document
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    TransientProcessingError,
    ConnectionError,
    TimeoutError,
)


@celery_app.task(
    bind=True,
    name="corerag.process_document",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def process_document_task(
    self: Any,
    document_id: str,
    file_path: str,
    tenant_id: str,
    file_type: str,
    request_id: str = "-",
) -> dict[str, object]:
    """Dokümanı sayfa sayfa okuyup chunk'lar, geçici hatada backoff ile dener ve temizler."""
    token = request_id_ctx.set(request_id)
    target = Path(file_path)
    should_cleanup_file = True

    try:
        if not target.is_file():
            logger.warning(
                "Document file missing for document_id=%s tenant_id=%s path=%s",
                document_id,
                tenant_id,
                target.name,
            )
            return {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "file_type": file_type,
                "file_exists": False,
                "size_bytes": 0,
                "page_count": 0,
                "chunk_count": 0,
                "status": "missing_file",
            }

        size_bytes = target.stat().st_size
        settings = get_settings()
        parsed = parse_and_chunk_document(
            file_path=target,
            document_id=document_id,
            tenant_id=tenant_id,
            file_type=file_type,
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
        )

        logger.info(
            "Processed document_id=%s tenant_id=%s type=%s pages=%d chunks=%d size=%d",
            document_id,
            tenant_id,
            file_type,
            parsed.page_count,
            len(parsed.chunks),
            size_bytes,
        )
        return {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "file_type": file_type,
            "file_exists": True,
            "size_bytes": size_bytes,
            "page_count": parsed.page_count,
            "chunk_count": len(parsed.chunks),
            "status": "processed",
        }
    except RETRYABLE_EXCEPTIONS as exc:
        current_retries = int(getattr(getattr(self, "request", None), "retries", 0) or 0)
        max_retries = int(getattr(self, "max_retries", 3) or 3)
        if current_retries < max_retries:
            # Sonraki retry denemesinin dosyayı tekrar okuyabilmesi için geçici silmeyi ertele
            should_cleanup_file = False
        logger.warning(
            "Transient error on document_id=%s (retry %d/%d): %s",
            document_id,
            current_retries,
            max_retries,
            type(exc).__name__,
        )
        raise
    finally:
        if should_cleanup_file:
            target.unlink(missing_ok=True)
        request_id_ctx.reset(token)


class CeleryDocumentTaskDispatcher(DocumentTaskDispatcher):
    """Doküman işleme görevlerini Celery/Redis kuyruğuna asenkron olarak iletir."""

    async def enqueue_document(
        self,
        *,
        document_id: str,
        file_path: Path,
        tenant_id: str,
        file_type: str,
        request_id: str,
    ) -> str:
        async_result = await asyncio.to_thread(
            process_document_task.delay,
            document_id=document_id,
            file_path=str(file_path),
            tenant_id=tenant_id,
            file_type=file_type,
            request_id=request_id,
        )
        return str(async_result.id)

    async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
        return await asyncio.to_thread(self._fetch_status_sync, task_id)

    @staticmethod
    def _fetch_status_sync(task_id: str) -> TaskStatusSnapshot:
        async_result = celery_app.AsyncResult(task_id)
        raw_state = str(async_result.state).upper()

        if raw_state == "SUCCESS":
            raw_payload = async_result.result
            payload = (
                dict(raw_payload) if isinstance(raw_payload, Mapping) else {"value": raw_payload}
            )
            return TaskStatusSnapshot(
                task_id=task_id,
                status="SUCCESS",
                result=payload,
                error=None,
            )

        if raw_state in {"FAILURE", "REVOKED"}:
            logger.warning(
                "Task %s finished with state=%s (%s)",
                task_id,
                raw_state,
                type(async_result.result).__name__,
            )
            return TaskStatusSnapshot(
                task_id=task_id,
                status="FAILURE",
                result=None,
                error="Document processing failed",
            )

        if raw_state == "STARTED":
            return TaskStatusSnapshot(task_id=task_id, status="STARTED")

        if raw_state == "RETRY":
            return TaskStatusSnapshot(task_id=task_id, status="RETRY")

        return TaskStatusSnapshot(task_id=task_id, status="PENDING")
