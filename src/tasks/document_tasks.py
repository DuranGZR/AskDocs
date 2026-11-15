import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from src.core.logging import request_id_ctx
from src.services.base import DocumentTaskDispatcher, TaskStatusSnapshot
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="corerag.process_document")
def process_document_task(
    document_id: str,
    file_path: str,
    tenant_id: str,
    file_type: str,
    request_id: str = "-",
) -> dict[str, object]:
    """Dokümanı arka planda işler (9. Günde PDF parsing ve Qdrant yazımı bağlanacak)."""
    token = request_id_ctx.set(request_id)
    try:
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
    finally:
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
