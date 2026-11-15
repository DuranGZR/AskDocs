from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api.dependencies import get_llm_client, get_vector_db
from src.core.config import Settings
from src.core.observability import (
    RAGTraceRecorder,
    configure_observability,
    estimate_messages_tokens,
    estimate_tokens,
    get_langfuse_client,
    record_worker_indexing_trace,
    set_langfuse_client,
    utc_now,
)
from src.services.base import (
    BaseLLMClient,
    BaseVectorDB,
    ChatMessage,
    DocumentChunk,
    ScoredChunk,
    VectorPoint,
)


class _FakeTrace:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.spans: list[dict[str, Any]] = []
        self.generations: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def span(self, **kwargs: Any) -> None:
        self.spans.append(kwargs)

    def generation(self, **kwargs: Any) -> None:
        self.generations.append(kwargs)

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.traces: list[_FakeTrace] = []
        self.flush_count = 0

    def trace(self, **kwargs: Any) -> _FakeTrace:
        trace = _FakeTrace(kwargs)
        self.traces.append(trace)
        return trace

    def flush(self) -> None:
        self.flush_count += 1


class _BrokenTrace:
    def span(self, **kwargs: Any) -> None:
        raise RuntimeError("Langfuse span failure")

    def generation(self, **kwargs: Any) -> None:
        raise RuntimeError("Langfuse generation failure")

    def update(self, **kwargs: Any) -> None:
        raise RuntimeError("Langfuse update failure")


class _BrokenLangfuseClient:
    def __init__(self, *, fail_on_trace: bool = False) -> None:
        self._fail_on_trace = fail_on_trace

    def trace(self, **kwargs: Any) -> _BrokenTrace:
        if self._fail_on_trace:
            raise RuntimeError("Langfuse trace server unreachable")
        return _BrokenTrace()

    def flush(self) -> None:
        raise RuntimeError("Langfuse flush failed")


class _StubLLMClient(BaseLLMClient):
    async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        return [[0.2, 0.4] for _ in texts]

    async def generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "Yıllık destek 15.000 TL'dir [Source 1]."

    async def stream_generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "Yıllık destek "
        yield "15.000 TL'dir."

    async def close(self) -> None:
        return None


class _StubVectorDB(BaseVectorDB):
    async def ensure_collection(self) -> None:
        return None

    async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
        return len(points)

    async def search(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        top_k: int = 5,
        score_threshold: float = 0.5,
    ) -> list[ScoredChunk]:
        chunk = DocumentChunk(
            document_id="doc-obs-1",
            tenant_id=tenant_id,
            page_number=2,
            chunk_index=0,
            text="Ev ofisi kurulum desteği 15.000 TL'dir.",
            file_type="pdf",
        )
        return [ScoredChunk(point_id="pt-1", score=0.91, chunk=chunk)]

    async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def reset_langfuse_singleton() -> Iterator[None]:
    """Testler arasında modül seviyesi Langfuse istemcisini sıfırlar."""
    set_langfuse_client(None)
    yield
    set_langfuse_client(None)


def test_estimate_tokens_and_messages_tokens() -> None:
    """Token kestirimi boş metinde 0, dolu metin ve mesajlarda pozitif sayı dönmelidir."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2

    messages = [
        ChatMessage(role="system", content="abcdefgh"),
        ChatMessage(role="user", content="abcd"),
    ]
    assert estimate_messages_tokens(messages) == (2 + 4) + (1 + 4)


def test_rag_trace_recorder_records_spans_ttft_and_tokens() -> None:
    """RAGTraceRecorder embedding, retrieval, TTFT ve token kullanımını kaydetmelidir."""
    fake_client = _FakeLangfuseClient()
    recorder = RAGTraceRecorder(
        trace_name="rag.query.stream",
        tenant_id="tenant-alpha",
        request_id="req-obs-1",
        query="Ev ofisi desteği nedir?",
        top_k=5,
        score_threshold=0.3,
        stream=True,
        client=fake_client,
    )

    recorder.record_embedding_span(
        model="gemini-embedding-001",
        query="Ev ofisi desteği nedir?",
        duration_ms=42.5,
        vector_dim=3072,
    )
    recorder.record_retrieval_span(
        tenant_id="tenant-alpha",
        top_k=5,
        score_threshold=0.3,
        duration_ms=11.2,
        citation_count=1,
        top_score=0.912345,
    )

    start_dt = utc_now()
    first_token_dt = start_dt + timedelta(milliseconds=180)
    end_dt = start_dt + timedelta(milliseconds=420)
    messages = [
        ChatMessage(role="system", content="Only use context."),
        ChatMessage(role="user", content="Ev ofisi desteği nedir?"),
    ]
    recorder.record_llm_generation(
        name="rag.llm_stream",
        model="gemini-3.5-flash-lite",
        messages=messages,
        output_text="15.000 TL'dir.",
        start_time=start_dt,
        completion_start_time=first_token_dt,
        end_time=end_dt,
        ttft_ms=180.0,
        total_ms=420.0,
    )

    assert len(fake_client.traces) == 1
    trace = fake_client.traces[0]
    assert trace.kwargs["name"] == "rag.query.stream"
    assert trace.kwargs["user_id"] == "tenant-alpha"
    assert trace.kwargs["session_id"] == "req-obs-1"

    assert len(trace.spans) == 2
    assert trace.spans[0]["name"] == "rag.embed_query"
    assert trace.spans[0]["output"] == {"vector_dim": 3072}
    assert trace.spans[0]["metadata"] == {"latency_ms": 42.5}
    assert trace.spans[1]["name"] == "rag.qdrant_search"
    assert trace.spans[1]["output"] == {"citation_count": 1, "top_score": 0.912345}

    assert len(trace.generations) == 1
    gen = trace.generations[0]
    assert gen["name"] == "rag.llm_stream"
    assert gen["model"] == "gemini-3.5-flash-lite"
    assert gen["completion_start_time"] == first_token_dt
    assert gen["usage"]["unit"] == "TOKENS"
    assert gen["usage"]["total"] == gen["usage"]["input"] + gen["usage"]["output"]
    assert gen["metadata"] == {"ttft_ms": 180.0, "latency_ms": 420.0}
    assert trace.updates[-1]["output"]["ttft_ms"] == 180.0


def test_record_worker_indexing_trace_records_chunk_metrics() -> None:
    """Celery worker indeksleme kaydı trace ve child span oluşturmalıdır."""
    fake_client = _FakeLangfuseClient()
    start_dt = utc_now()
    end_dt = start_dt + timedelta(milliseconds=950)

    record_worker_indexing_trace(
        document_id="doc-worker-1",
        tenant_id="tenant-alpha",
        request_id="req-w1",
        file_type="pdf",
        size_bytes=31144,
        page_count=3,
        chunk_count=11,
        upserted_count=11,
        embedding_model="gemini-embedding-001",
        estimated_tokens=850,
        duration_ms=950.4,
        start_time=start_dt,
        end_time=end_dt,
        status="processed",
        client=fake_client,
    )

    assert len(fake_client.traces) == 1
    trace = fake_client.traces[0]
    assert trace.kwargs["name"] == "worker.process_document"
    assert trace.kwargs["user_id"] == "tenant-alpha"
    assert trace.kwargs["output"]["upserted_count"] == 11
    assert len(trace.spans) == 1
    assert trace.spans[0]["name"] == "worker.index_document_chunks"
    assert trace.spans[0]["input"]["estimated_tokens"] == 850


def test_observability_fail_open_and_disabled_configuration(
    test_settings: Settings,
) -> None:
    """Langfuse kapalı veya hatalı olduğunda sistem hata fırlatmadan devam etmelidir."""
    disabled_settings = test_settings.model_copy(update={"LANGFUSE_ENABLED": False})
    assert configure_observability(disabled_settings) is None
    assert get_langfuse_client() is None

    for fail_on_trace in (True, False):
        broken_client = _BrokenLangfuseClient(fail_on_trace=fail_on_trace)
        recorder = RAGTraceRecorder(
            trace_name="rag.query",
            tenant_id="tenant-fail",
            request_id="req-fail",
            query="Test",
            top_k=3,
            score_threshold=0.5,
            stream=False,
            client=broken_client,
        )
        recorder.record_embedding_span(
            model="m",
            query="Test",
            duration_ms=1.0,
            vector_dim=8,
        )
        recorder.record_retrieval_span(
            tenant_id="tenant-fail",
            top_k=3,
            score_threshold=0.5,
            duration_ms=1.0,
            citation_count=0,
            top_score=None,
        )
        now = utc_now()
        recorder.record_llm_generation(
            name="rag.llm_generate",
            model="m",
            messages=[ChatMessage(role="user", content="Test")],
            output_text="ok",
            start_time=now,
            completion_start_time=now,
            end_time=now,
            ttft_ms=1.0,
            total_ms=1.0,
        )
        record_worker_indexing_trace(
            document_id="d1",
            tenant_id="tenant-fail",
            request_id="req-fail",
            file_type="pdf",
            size_bytes=100,
            page_count=1,
            chunk_count=1,
            upserted_count=1,
            embedding_model="m",
            estimated_tokens=5,
            duration_ms=1.0,
            start_time=now,
            end_time=now,
            status="processed",
            client=broken_client,
        )


async def test_query_and_stream_endpoints_survive_broken_langfuse_client(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """Langfuse istemcisi hata verse bile /query ve /query/stream 200 OK dönmelidir."""
    app.dependency_overrides[get_llm_client] = lambda: _StubLLMClient()
    app.dependency_overrides[get_vector_db] = lambda: _StubVectorDB()
    set_langfuse_client(_BrokenLangfuseClient(fail_on_trace=False))

    resp_json = await client.post(
        "/api/v1/query",
        json={"query": "Destek ne kadar?", "tenant_id": "t1", "stream": False},
    )
    assert resp_json.status_code == 200
    assert "15.000 TL" in resp_json.json()["answer"]

    resp_stream = await client.post(
        "/api/v1/query/stream",
        json={"query": "Destek ne kadar?", "tenant_id": "t1", "stream": True},
    )
    assert resp_stream.status_code == 200
    assert "event: token" in resp_stream.text
    assert "event: citations" in resp_stream.text
    assert "data: [DONE]" in resp_stream.text
