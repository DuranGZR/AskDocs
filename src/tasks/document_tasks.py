import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.core.config import Settings, get_settings
from src.core.exceptions import ServiceUnavailableError
from src.core.logging import request_id_ctx
from src.services.base import (
    DocumentChunk,
    DocumentTaskDispatcher,
    SyncBaseLLMClient,
    SyncBaseVectorDB,
    TaskStatusSnapshot,
    VectorPoint,
)
from src.services.llm_service import SyncOpenAICompatibleLLMClient
from src.services.pdf_parser import TransientProcessingError, parse_and_chunk_document
from src.services.qdrant_service import SyncQdrantService, build_chunk_point_id
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    TransientProcessingError,
    ServiceUnavailableError,
    ConnectionError,
    TimeoutError,
)


def create_worker_llm_client(settings: Settings) -> SyncBaseLLMClient:
    """Celery worker için senkron OpenAI-uyumlu embedding istemcisi oluşturur."""
    return SyncOpenAICompatibleLLMClient(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        embedding_model=settings.EMBEDDING_MODEL,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
    )


def create_worker_vector_db(settings: Settings) -> SyncBaseVectorDB:
    """Celery worker için senkron Qdrant vektör veritabanı istemcisi oluşturur."""
    return SyncQdrantService(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
        vector_size=settings.QDRANT_VECTOR_SIZE,
    )


def index_document_chunks(
    *,
    chunks: Sequence[DocumentChunk],
    llm_client: SyncBaseLLMClient,
    vector_db: SyncBaseVectorDB,
    batch_size: int = 32,
) -> int:
    """Parçaları batch halinde vektörleştirip deterministik UUID5 ile Qdrant'a yazar."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not chunks:
        return 0

    vector_db.ensure_collection()
    total_upserted = 0

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = llm_client.embed_texts(texts=[chunk.text for chunk in batch])
        if len(vectors) != len(batch):
            raise ServiceUnavailableError(
                "Embedding vector count mismatch received from AI server."
            )

        points = [
            VectorPoint(
                point_id=build_chunk_point_id(chunk.document_id, chunk.chunk_index),
                vector=vector,
                chunk=chunk,
            )
            for chunk, vector in zip(batch, vectors, strict=True)
        ]
        total_upserted += vector_db.upsert(points=points)

    return total_upserted


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
    """Dokümanı okur, batch vektörleştirip Qdrant'a yazar, hatada retry eder ve temizler."""
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
                "upserted_count": 0,
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

        upserted_count = 0
        if parsed.chunks:
            llm_client = create_worker_llm_client(settings)
            try:
                vector_db = create_worker_vector_db(settings)
                try:
                    upserted_count = index_document_chunks(
                        chunks=parsed.chunks,
                        llm_client=llm_client,
                        vector_db=vector_db,
                        batch_size=settings.EMBEDDING_BATCH_SIZE,
                    )
                finally:
                    vector_db.close()
            finally:
                llm_client.close()

        logger.info(
            "Processed document_id=%s tenant_id=%s type=%s pages=%d chunks=%d upserted=%d size=%d",
            document_id,
            tenant_id,
            file_type,
            parsed.page_count,
            len(parsed.chunks),
            upserted_count,
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
            "upserted_count": upserted_count,
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
