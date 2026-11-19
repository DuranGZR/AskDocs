import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.core.circuit_breaker import SyncCircuitBreaker
from src.core.config import Settings, get_settings
from src.core.exceptions import ServiceUnavailableError
from src.core.logging import request_id_ctx
from src.core.observability import (
    configure_observability,
    estimate_tokens,
    get_langfuse_client,
    observe,
    record_worker_indexing_trace,
    utc_now,
)
from src.services.base import (
    DocumentChunk,
    DocumentTaskDispatcher,
    SyncBaseLLMClient,
    SyncBaseVectorDB,
    TaskStatusSnapshot,
    VectorPoint,
)
from src.services.llm_service import (
    QuotaExhaustedError,
    SyncOpenAICompatibleLLMClient,
    infer_provider_label,
)
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


_WORKER_BREAKERS: dict[tuple[str, bool, int, float, int, int], SyncCircuitBreaker] = {}
_WORKER_BREAKERS_LOCK = threading.Lock()


def _get_worker_circuit_breaker(name: str, settings: Settings) -> SyncCircuitBreaker:
    """Worker süreci içinde yapılandırmayla uyumlu paylaşımlı SyncCircuitBreaker döner."""
    cache_key = (
        name,
        settings.CIRCUIT_BREAKER_ENABLED,
        settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        settings.CIRCUIT_BREAKER_RECOVERY_SECONDS,
        settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS,
        settings.CIRCUIT_BREAKER_HALF_OPEN_SUCCESS_THRESHOLD,
    )
    with _WORKER_BREAKERS_LOCK:
        breaker = _WORKER_BREAKERS.get(cache_key)
        if breaker is None:
            breaker = SyncCircuitBreaker(
                name=name,
                enabled=settings.CIRCUIT_BREAKER_ENABLED,
                failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                recovery_timeout_seconds=settings.CIRCUIT_BREAKER_RECOVERY_SECONDS,
                half_open_max_calls=settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS,
                half_open_success_threshold=settings.CIRCUIT_BREAKER_HALF_OPEN_SUCCESS_THRESHOLD,
            )
            _WORKER_BREAKERS[cache_key] = breaker
        return breaker


def create_worker_llm_client(settings: Settings) -> SyncBaseLLMClient:
    """Celery worker için senkron OpenAI-uyumlu embedding istemcisi oluşturur."""
    return SyncOpenAICompatibleLLMClient(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        embedding_model=settings.EMBEDDING_MODEL,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
        circuit_breaker=_get_worker_circuit_breaker("worker.llm", settings),
    )


def create_worker_vector_db(settings: Settings) -> SyncBaseVectorDB:
    """Celery worker için senkron Qdrant vektör veritabanı istemcisi oluşturur."""
    return SyncQdrantService(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
        vector_size=settings.QDRANT_VECTOR_SIZE,
        circuit_breaker=_get_worker_circuit_breaker("worker.qdrant", settings),
    )


def _rollback_partial_document_vectors(
    vector_db: SyncBaseVectorDB,
    *,
    tenant_id: str,
    document_id: str,
) -> None:
    """Kalıcı görev hatasında veya zaman aşımında Qdrant'taki kısmi vektörleri temizler."""
    try:
        vector_db.delete_by_document(tenant_id=tenant_id, document_id=document_id)
        logger.info(
            "Rolled back partial vectors for failed document_id=%s tenant_id=%s",
            document_id,
            tenant_id,
        )
    except Exception as cleanup_exc:
        logger.warning(
            "Failed to roll back partial vectors for document_id=%s tenant_id=%s: %s",
            document_id,
            tenant_id,
            type(cleanup_exc).__name__,
        )


def _update_task_progress(task: Any, meta: dict[str, object]) -> None:
    """Celery görev durumunu Redis arka ucuna canlı ilerleme/kota metasıyla yazar."""
    task_id = getattr(getattr(task, "request", None), "id", None)
    if not task_id or not hasattr(task, "update_state"):
        return
    try:
        task.update_state(state="STARTED", meta=meta)
    except Exception:
        pass


@observe(name="worker.index_document_chunks", capture_input=False, capture_output=False)
def index_document_chunks(
    *,
    chunks: Sequence[DocumentChunk],
    llm_client: SyncBaseLLMClient,
    vector_db: SyncBaseVectorDB,
    batch_size: int = 32,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Parçaları batch halinde vektörleştirip deterministik UUID5 ile Qdrant'a yazar."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not chunks:
        return 0

    vector_db.ensure_collection()
    total_upserted = 0
    total_chunks = len(chunks)
    if on_progress is not None:
        on_progress(0, total_chunks)

    for start in range(0, total_chunks, batch_size):
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
        if on_progress is not None:
            on_progress(total_upserted, total_chunks)

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
    filename: str = "",
) -> dict[str, object]:
    """Dokümanı okur, batch vektörleştirip Qdrant'a yazar, hatada retry eder ve temizler."""
    token = request_id_ctx.set(request_id)
    target = Path(file_path)
    should_cleanup_file = True
    task_start = utc_now()
    t_task = time.perf_counter()
    size_bytes = 0
    page_count = 0
    chunk_count = 0
    upserted_count = 0
    estimated_tokens = 0
    embedding_model = ""

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
        embedding_model = settings.EMBEDDING_MODEL
        if settings.LANGFUSE_ENABLED and get_langfuse_client() is None:
            configure_observability(settings)

        parsed = parse_and_chunk_document(
            file_path=target,
            document_id=document_id,
            tenant_id=tenant_id,
            file_type=file_type,
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            filename=filename,
        )
        page_count = parsed.page_count
        chunk_count = len(parsed.chunks)
        estimated_tokens = sum(estimate_tokens(chunk.text) for chunk in parsed.chunks)

        if parsed.chunks:
            current_chunks_done = 0
            provider_label = infer_provider_label(settings.LLM_BASE_URL)

            def _on_progress(done_count: int, total_count: int) -> None:
                nonlocal current_chunks_done
                current_chunks_done = done_count
                _update_task_progress(
                    self,
                    {
                        "phase": "embedding",
                        "page_count": page_count,
                        "chunks_done": done_count,
                        "chunks_total": total_count,
                        "provider": provider_label,
                    },
                )

            def _on_rate_limit(
                delay_sec: float,
                attempt: int,
                max_retries: int,
                provider: str,
            ) -> None:
                wait_sec = max(1, round(delay_sec))
                _update_task_progress(
                    self,
                    {
                        "phase": "rate_limited",
                        "page_count": page_count,
                        "chunks_done": current_chunks_done,
                        "chunks_total": chunk_count,
                        "wait_seconds": wait_sec,
                        "resume_at_epoch_ms": int((time.time() + delay_sec) * 1000),
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "provider": provider,
                    },
                )

            llm_client = create_worker_llm_client(settings)
            llm_client.set_rate_limit_callback(_on_rate_limit)
            try:
                vector_db = create_worker_vector_db(settings)
                try:
                    upserted_count = index_document_chunks(
                        chunks=parsed.chunks,
                        llm_client=llm_client,
                        vector_db=vector_db,
                        batch_size=settings.EMBEDDING_BATCH_SIZE,
                        on_progress=_on_progress,
                    )
                except BaseException as exc:
                    current_retries = int(
                        getattr(getattr(self, "request", None), "retries", 0) or 0
                    )
                    max_retries = int(getattr(self, "max_retries", 3) or 3)
                    will_retry = (
                        isinstance(exc, RETRYABLE_EXCEPTIONS) and current_retries < max_retries
                    )
                    if not will_retry:
                        _rollback_partial_document_vectors(
                            vector_db,
                            tenant_id=tenant_id,
                            document_id=document_id,
                        )
                    raise
                finally:
                    vector_db.close()
            finally:
                llm_client.close()

        result_status = "processed" if chunk_count > 0 else "no_text_extracted"
        if chunk_count == 0:
            logger.warning(
                "No extractable text found for document_id=%s tenant_id=%s type=%s pages=%d",
                document_id,
                tenant_id,
                file_type,
                page_count,
            )

        record_worker_indexing_trace(
            document_id=document_id,
            tenant_id=tenant_id,
            request_id=request_id,
            file_type=file_type,
            size_bytes=size_bytes,
            page_count=page_count,
            chunk_count=chunk_count,
            upserted_count=upserted_count,
            embedding_model=embedding_model,
            estimated_tokens=estimated_tokens,
            duration_ms=(time.perf_counter() - t_task) * 1000.0,
            start_time=task_start,
            end_time=utc_now(),
            status=result_status,
        )
        logger.info(
            "Processed document_id=%s tenant_id=%s type=%s pages=%d chunks=%d "
            "upserted=%d size=%d status=%s",
            document_id,
            tenant_id,
            file_type,
            page_count,
            chunk_count,
            upserted_count,
            size_bytes,
            result_status,
        )
        return {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "file_type": file_type,
            "file_exists": True,
            "size_bytes": size_bytes,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "upserted_count": upserted_count,
            "status": result_status,
        }
    except QuotaExhaustedError as exc:
        record_worker_indexing_trace(
            document_id=document_id,
            tenant_id=tenant_id,
            request_id=request_id,
            file_type=file_type,
            size_bytes=size_bytes,
            page_count=page_count,
            chunk_count=chunk_count,
            upserted_count=0,
            embedding_model=embedding_model,
            estimated_tokens=estimated_tokens,
            duration_ms=(time.perf_counter() - t_task) * 1000.0,
            start_time=task_start,
            end_time=utc_now(),
            status="quota_exhausted",
            error=str(exc),
        )
        logger.warning(
            "Daily quota exhausted for document_id=%s tenant_id=%s: %s",
            document_id,
            tenant_id,
            str(exc),
        )
        return {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "file_type": file_type,
            "file_exists": True,
            "size_bytes": size_bytes,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "upserted_count": 0,
            "status": "quota_exhausted",
            "error": str(exc),
        }
    except RETRYABLE_EXCEPTIONS as exc:
        current_retries = int(getattr(getattr(self, "request", None), "retries", 0) or 0)
        max_retries = int(getattr(self, "max_retries", 3) or 3)
        if current_retries < max_retries:
            # Sonraki retry denemesinin dosyayı tekrar okuyabilmesi için geçici silmeyi ertele
            should_cleanup_file = False
        record_worker_indexing_trace(
            document_id=document_id,
            tenant_id=tenant_id,
            request_id=request_id,
            file_type=file_type,
            size_bytes=size_bytes,
            page_count=page_count,
            chunk_count=chunk_count,
            upserted_count=upserted_count,
            embedding_model=embedding_model,
            estimated_tokens=estimated_tokens,
            duration_ms=(time.perf_counter() - t_task) * 1000.0,
            start_time=task_start,
            end_time=utc_now(),
            status="retrying" if current_retries < max_retries else "failed",
            error=f"{type(exc).__name__}: {exc}",
        )
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
        filename: str = "",
    ) -> str:
        delay_kwargs: dict[str, str] = {
            "document_id": document_id,
            "file_path": str(file_path),
            "tenant_id": tenant_id,
            "file_type": file_type,
            "request_id": request_id,
        }
        if filename:
            delay_kwargs["filename"] = filename

        async_result = await asyncio.to_thread(
            process_document_task.delay,
            **delay_kwargs,
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
            raw_info = getattr(async_result, "info", None) or async_result.result
            meta = dict(raw_info) if isinstance(raw_info, Mapping) else None
            return TaskStatusSnapshot(task_id=task_id, status="STARTED", result=meta)

        if raw_state == "RETRY":
            return TaskStatusSnapshot(task_id=task_id, status="RETRY")

        return TaskStatusSnapshot(task_id=task_id, status="PENDING")
