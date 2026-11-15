import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from langfuse import Langfuse
from langfuse.decorators import langfuse_context, observe

from src.core.config import Settings
from src.services.base import ChatMessage

logger = logging.getLogger(__name__)

_langfuse_client: Any = None

try:
    langfuse_context.configure(enabled=False)
except Exception:  # pragma: no cover - savunmacı fail-open koruması
    pass


def utc_now() -> datetime:
    """UTC zaman damgalı güncel datetime nesnesi döner."""
    return datetime.now(UTC)


def estimate_tokens(text: str) -> int:
    """Harici tokenizer olmadan metin uzunluğundan hızlı yaklaşık token sayısı hesaplar."""
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def estimate_messages_tokens(messages: Sequence[ChatMessage]) -> int:
    """Mesaj listesindeki toplam yaklaşık girdi token sayısını hesaplar."""
    return sum(estimate_tokens(message.content) + 4 for message in messages)


def get_langfuse_client() -> Any:
    """Aktif Langfuse istemci örneğini (veya kapalıysa None) döndürür."""
    return _langfuse_client


def set_langfuse_client(client: Any) -> None:
    """Test veya yaşam döngüsü yönetimi için Langfuse istemcisini atar."""
    global _langfuse_client
    _langfuse_client = client


def configure_observability(settings: Settings) -> Any:
    """Ayarlar doğrultusunda Langfuse istemcisini ve @observe bağlamını fail-open başlatır."""
    global _langfuse_client

    if not settings.LANGFUSE_ENABLED:
        _langfuse_client = None
        try:
            langfuse_context.configure(enabled=False)
        except Exception as exc:  # pragma: no cover - savunmacı fail-open koruması
            logger.warning("Failed to disable Langfuse context cleanly: %s", exc)
        return None

    secret_key = settings.LANGFUSE_SECRET_KEY.get_secret_value()
    os.environ["LANGFUSE_HOST"] = settings.LANGFUSE_HOST
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.LANGFUSE_PUBLIC_KEY
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key

    try:
        client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=secret_key,
            host=settings.LANGFUSE_HOST,
            environment=settings.APP_ENV,
            timeout=5,
            max_retries=1,
            enabled=True,
        )
        langfuse_context.configure(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=secret_key,
            host=settings.LANGFUSE_HOST,
            environment=settings.APP_ENV,
            timeout=5,
            max_retries=1,
            enabled=True,
        )
        _langfuse_client = client
        logger.info("Configured Langfuse observability (host=%s)", settings.LANGFUSE_HOST)
        return client
    except Exception as exc:
        _langfuse_client = None
        logger.warning("Langfuse initialization skipped (fail-open): %s", exc)
        return None


def flush_observability() -> None:
    """Bekleyen tüm Langfuse trace ve span verilerini hata fırlatmadan gönderir."""
    client = get_langfuse_client()
    if client is not None:
        try:
            client.flush()
        except Exception as exc:
            logger.warning("Langfuse client flush failed (fail-open): %s", exc)

    try:
        langfuse_context.flush()
    except Exception as exc:
        logger.warning("Langfuse context flush failed (fail-open): %s", exc)


def shutdown_observability() -> None:
    """Uygulama kapanışında Langfuse kuyruğunu boşaltır ve arka plan işçisini kapatır."""
    global _langfuse_client
    client = _langfuse_client
    flush_observability()
    if client is not None:
        try:
            shutdown_fn = getattr(client, "shutdown", None)
            if callable(shutdown_fn):
                shutdown_fn()
        except Exception as exc:
            logger.warning("Langfuse client shutdown failed (fail-open): %s", exc)
    try:
        langfuse_context.configure(enabled=False)
    except Exception as exc:  # pragma: no cover - savunmacı fail-open koruması
        logger.warning("Failed to reset Langfuse context cleanly: %s", exc)
    _langfuse_client = None


def _safe_current_trace_id() -> str | None:
    """Aktif @observe izi varsa kimliğini güvenli şekilde döner."""
    try:
        trace_id = langfuse_context.get_current_trace_id()
        return str(trace_id) if trace_id else None
    except Exception:
        return None


def _span_times(
    duration_ms: float,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[datetime, datetime]:
    resolved_end = end_time or utc_now()
    resolved_start = start_time or (resolved_end - timedelta(milliseconds=max(0.0, duration_ms)))
    return resolved_start, resolved_end


class RAGTraceRecorder:
    """Tek bir RAG sorgu isteğinin embedding, Qdrant arama ve LLM/TTFT metriklerini kaydeder."""

    def __init__(
        self,
        *,
        trace_name: str,
        tenant_id: str,
        request_id: str,
        query: str,
        top_k: int,
        score_threshold: float,
        stream: bool,
        client: Any = None,
    ) -> None:
        self._trace: Any = None
        resolved_client = client if client is not None else get_langfuse_client()
        if resolved_client is None:
            return

        input_payload = {
            "query": query,
            "top_k": top_k,
            "score_threshold": score_threshold,
        }
        metadata = {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "stream": stream,
        }

        current_trace_id = _safe_current_trace_id()
        if current_trace_id:
            try:
                langfuse_context.update_current_trace(
                    name=trace_name,
                    user_id=tenant_id,
                    session_id=request_id,
                    input=input_payload,
                    metadata=metadata,
                )
            except Exception as exc:
                logger.warning("Langfuse trace context update failed (fail-open): %s", exc)

        trace_kwargs: dict[str, Any] = {
            "name": trace_name,
            "user_id": tenant_id,
            "session_id": request_id,
            "input": input_payload,
            "metadata": metadata,
        }
        if current_trace_id:
            trace_kwargs["id"] = current_trace_id

        try:
            self._trace = resolved_client.trace(**trace_kwargs)
        except Exception as exc:
            logger.warning("Langfuse trace creation failed (fail-open): %s", exc)
            self._trace = None

    def record_embedding_span(
        self,
        *,
        model: str,
        query: str,
        duration_ms: float,
        vector_dim: int,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Soru vektörleştirme adımını ve tahmini girdi token sayısını span olarak kaydeder."""
        if self._trace is None:
            return
        resolved_start, resolved_end = _span_times(duration_ms, start_time, end_time)
        try:
            self._trace.span(
                name="rag.embed_query",
                start_time=resolved_start,
                end_time=resolved_end,
                input={
                    "model": model,
                    "query_length": len(query),
                    "estimated_tokens": estimate_tokens(query),
                },
                output={"vector_dim": vector_dim},
                metadata={"latency_ms": round(duration_ms, 2)},
                level="ERROR" if error else "DEFAULT",
                status_message=error,
            )
        except Exception as exc:
            logger.warning("Langfuse embedding span failed (fail-open): %s", exc)

    def record_retrieval_span(
        self,
        *,
        tenant_id: str,
        top_k: int,
        score_threshold: float,
        duration_ms: float,
        citation_count: int,
        top_score: float | None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Qdrant vektör arama gecikmesini ve bulunan kaynak sayısını span olarak kaydeder."""
        if self._trace is None:
            return
        resolved_start, resolved_end = _span_times(duration_ms, start_time, end_time)
        try:
            self._trace.span(
                name="rag.qdrant_search",
                start_time=resolved_start,
                end_time=resolved_end,
                input={
                    "tenant_id": tenant_id,
                    "top_k": top_k,
                    "score_threshold": score_threshold,
                },
                output={
                    "citation_count": citation_count,
                    "top_score": round(top_score, 6) if top_score is not None else None,
                },
                metadata={"latency_ms": round(duration_ms, 2)},
                level="ERROR" if error else "DEFAULT",
                status_message=error,
            )
        except Exception as exc:
            logger.warning("Langfuse retrieval span failed (fail-open): %s", exc)

    def record_llm_generation(
        self,
        *,
        name: str,
        model: str,
        messages: Sequence[ChatMessage],
        output_text: str,
        start_time: datetime,
        completion_start_time: datetime | None,
        end_time: datetime,
        ttft_ms: float | None,
        total_ms: float,
        error: str | None = None,
    ) -> None:
        """LLM üretimini TTFT, toplam gecikme ve yaklaşık token kullanımıyla kaydeder."""
        if self._trace is None:
            return

        prompt_tokens = estimate_messages_tokens(messages)
        completion_tokens = estimate_tokens(output_text)
        total_tokens = prompt_tokens + completion_tokens
        rounded_ttft = round(ttft_ms, 2) if ttft_ms is not None else None
        rounded_latency = round(total_ms, 2)

        try:
            self._trace.generation(
                name=name,
                model=model,
                start_time=start_time,
                completion_start_time=completion_start_time,
                end_time=end_time,
                input=[{"role": msg.role, "content": msg.content} for msg in messages],
                output=output_text if output_text else None,
                usage={
                    "input": prompt_tokens,
                    "output": completion_tokens,
                    "total": total_tokens,
                    "unit": "TOKENS",
                },
                metadata={
                    "ttft_ms": rounded_ttft,
                    "latency_ms": rounded_latency,
                },
                level="ERROR" if error else "DEFAULT",
                status_message=error,
            )
            self._trace.update(
                output={
                    "answer": output_text,
                    "ttft_ms": rounded_ttft,
                    "latency_ms": rounded_latency,
                    "error": error,
                }
            )
        except Exception as exc:
            logger.warning("Langfuse LLM generation record failed (fail-open): %s", exc)


def record_worker_indexing_trace(
    *,
    document_id: str,
    tenant_id: str,
    request_id: str,
    file_type: str,
    size_bytes: int,
    page_count: int,
    chunk_count: int,
    upserted_count: int,
    embedding_model: str,
    estimated_tokens: int,
    duration_ms: float,
    start_time: datetime,
    end_time: datetime,
    status: str,
    error: str | None = None,
    client: Any = None,
) -> None:
    """Celery doküman indeksleme işinin süre, chunk ve token özetini Langfuse'a yazar."""
    resolved_client = client if client is not None else get_langfuse_client()
    if resolved_client is None:
        return

    try:
        trace = resolved_client.trace(
            name="worker.process_document",
            user_id=tenant_id,
            session_id=request_id,
            input={
                "document_id": document_id,
                "file_type": file_type,
                "size_bytes": size_bytes,
            },
            output={
                "status": status,
                "page_count": page_count,
                "chunk_count": chunk_count,
                "upserted_count": upserted_count,
                "error": error,
            },
            metadata={
                "request_id": request_id,
                "embedding_model": embedding_model,
                "estimated_tokens": estimated_tokens,
                "latency_ms": round(duration_ms, 2),
            },
        )
        trace.span(
            name="worker.index_document_chunks",
            start_time=start_time,
            end_time=end_time,
            input={
                "embedding_model": embedding_model,
                "chunk_count": chunk_count,
                "estimated_tokens": estimated_tokens,
            },
            output={"upserted_count": upserted_count},
            metadata={"latency_ms": round(duration_ms, 2)},
            level="ERROR" if error else "DEFAULT",
            status_message=error,
        )
    except Exception as exc:
        logger.warning("Langfuse worker trace failed (fail-open): %s", exc)
        return

    if client is None:
        flush_observability()


__all__ = [
    "RAGTraceRecorder",
    "configure_observability",
    "estimate_messages_tokens",
    "estimate_tokens",
    "flush_observability",
    "get_langfuse_client",
    "langfuse_context",
    "observe",
    "record_worker_indexing_trace",
    "set_langfuse_client",
    "shutdown_observability",
    "utc_now",
]
