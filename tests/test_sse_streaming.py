import json
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient

from src.api.dependencies import get_llm_client, get_vector_db
from src.core.exceptions import ServiceUnavailableError
from src.services.base import (
    BaseLLMClient,
    BaseVectorDB,
    ChatMessage,
    DocumentChunk,
    ScoredChunk,
    VectorPoint,
)
from src.services.llm_service import AsyncOpenAICompatibleLLMClient
from src.services.qdrant_service import AsyncQdrantService, build_chunk_point_id


def _parse_sse_events(raw_stream: str) -> list[tuple[str, str]]:
    """Ham SSE metnini (event_adi, data_icerigi) listesine ayrıştırır."""
    events: list[tuple[str, str]] = []
    blocks = [
        block.strip() for block in raw_stream.replace("\r\n", "\n").split("\n\n") if block.strip()
    ]
    for block in blocks:
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        events.append((event_name, "\n".join(data_lines)))
    return events


@pytest.mark.parametrize(
    ("endpoint", "stream_flag"),
    [
        ("/api/v1/query", True),
        ("/api/v1/query/stream", False),
    ],
)
async def test_query_stream_emits_tokens_citations_and_done_in_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    endpoint: str,
    stream_flag: bool,
) -> None:
    """SSE akışı sırayla token'ları, sonunda tenant izole citations'ı ve [DONE] dönmelidir."""
    qdrant_client = AsyncQdrantClient(location=":memory:")
    vector_db = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=qdrant_client,
    )
    await vector_db.ensure_collection()

    await vector_db.upsert(
        points=[
            VectorPoint(
                point_id=build_chunk_point_id("doc-stream-1", 2),
                vector=[1.0, 0.0, 0.0, 0.0],
                chunk=DocumentChunk(
                    document_id="doc-stream-1",
                    tenant_id="tenant_alpha",
                    page_number=4,
                    chunk_index=2,
                    text="Yıllık izin süresi kıdeme bağlı olarak 20 iş günüdür.",
                    file_type="pdf",
                ),
            ),
            VectorPoint(
                point_id=build_chunk_point_id("doc-secret-beta", 0),
                vector=[1.0, 0.0, 0.0, 0.0],
                chunk=DocumentChunk(
                    document_id="doc-secret-beta",
                    tenant_id="tenant_beta",
                    page_number=1,
                    chunk_index=0,
                    text="Tenant Beta gizli verisi.",
                    file_type="pdf",
                ),
            ),
        ]
    )

    sse_upstream = "\n".join(
        [
            'data: {"choices": [{"delta": {"role": "assistant"}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "Yıllık "}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "izin "}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "20 gündür."}}]}',
            "",
            "data: [DONE]",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/embeddings"):
            return httpx.Response(
                status_code=200,
                json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]},
            )
        if str(request.url).endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            assert body["stream"] is True
            assert body["temperature"] == 0.0
            return httpx.Response(
                status_code=200,
                text=sse_upstream,
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(status_code=404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm_client = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        client=http_client,
    )

    app.dependency_overrides[get_vector_db] = lambda: vector_db
    app.dependency_overrides[get_llm_client] = lambda: llm_client

    try:
        async with client.stream(
            "POST",
            endpoint,
            json={
                "query": "Yıllık izin süresi kaç gündür?",
                "tenant_id": "tenant_alpha",
                "stream": stream_flag,
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            raw_bytes = await response.aread()

        events = _parse_sse_events(raw_bytes.decode("utf-8"))
        assert [name for name, _ in events] == [
            "token",
            "token",
            "token",
            "citations",
            "done",
        ]

        deltas = [json.loads(data)["delta"] for name, data in events if name == "token"]
        assert "".join(deltas) == "Yıllık izin 20 gündür."

        citations_payload = json.loads(events[3][1])
        assert citations_payload["event"] == "citations"
        assert citations_payload["query"] == "Yıllık izin süresi kaç gündür?"
        assert citations_payload["tenant_id"] == "tenant_alpha"
        citations = citations_payload["citations"]
        assert len(citations) == 1
        assert citations[0]["document_id"] == "doc-stream-1"
        assert citations[0]["page_number"] == 4
        assert citations[0]["chunk_index"] == 2
        assert citations[0]["file_type"] == "pdf"
        assert citations[0]["score"] >= 0.99

        assert events[4] == ("done", "[DONE]")
    finally:
        await http_client.aclose()
        await vector_db.close()


async def test_query_stream_emits_sse_error_event_on_mid_stream_failure(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """Akış başladıktan sonra oluşan kesintide SSE error olayı ve request_id iletilmelidir."""

    class _MidFailLLM(BaseLLMClient):
        async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0, 0.0]]

        async def generate(
            self,
            *,
            messages: Sequence[ChatMessage],
            temperature: float = 0.0,
            max_tokens: int | None = None,
        ) -> str:
            return ""

        async def stream_generate(
            self,
            *,
            messages: Sequence[ChatMessage],
            temperature: float = 0.0,
            max_tokens: int | None = None,
        ) -> AsyncIterator[str]:
            yield "İlk kelime "
            raise ServiceUnavailableError("AI inference service is currently unavailable.")

        async def close(self) -> None:
            return None

    class _StaticVectorDB(BaseVectorDB):
        async def ensure_collection(self) -> None:
            return None

        async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
            return 0

        async def search(
            self,
            *,
            query_vector: Sequence[float],
            tenant_id: str,
            top_k: int = 5,
            score_threshold: float = 0.5,
        ) -> list[ScoredChunk]:
            return [
                ScoredChunk(
                    point_id="pt-1",
                    score=0.95,
                    chunk=DocumentChunk(
                        document_id="doc-1",
                        tenant_id=tenant_id,
                        page_number=1,
                        chunk_index=0,
                        text="Örnek içerik.",
                        file_type="pdf",
                    ),
                )
            ]

        async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
            return None

        async def close(self) -> None:
            return None

    app.dependency_overrides[get_llm_client] = _MidFailLLM
    app.dependency_overrides[get_vector_db] = _StaticVectorDB

    async with client.stream(
        "POST",
        "/api/v1/query/stream",
        json={"query": "Soru", "tenant_id": "tenant_a"},
        headers={"X-Request-ID": "req-mid-stream-fail"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-request-id"] == "req-mid-stream-fail"
        raw_bytes = await response.aread()

    events = _parse_sse_events(raw_bytes.decode("utf-8"))
    assert [name for name, _ in events] == ["token", "error", "done"]
    assert json.loads(events[0][1]) == {"event": "token", "delta": "İlk kelime "}

    error_payload = json.loads(events[1][1])
    assert error_payload == {
        "event": "error",
        "code": "service_unavailable",
        "message": "AI inference service is currently unavailable.",
        "request_id": "req-mid-stream-fail",
    }
    assert events[2] == ("done", "[DONE]")


async def test_query_stream_returns_503_json_when_pre_stream_search_fails(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """Akış açılmadan önce embedding veya Qdrant araması çökerse HTTP 503 JSON dönmelidir."""

    class _EmbedReadyLLM(BaseLLMClient):
        async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0, 0.0]]

        async def generate(
            self,
            *,
            messages: Sequence[ChatMessage],
            temperature: float = 0.0,
            max_tokens: int | None = None,
        ) -> str:
            return ""

        async def stream_generate(
            self,
            *,
            messages: Sequence[ChatMessage],
            temperature: float = 0.0,
            max_tokens: int | None = None,
        ) -> AsyncIterator[str]:
            if False:
                yield ""

        async def close(self) -> None:
            return None

    class _FailingSearchVectorDB(BaseVectorDB):
        async def ensure_collection(self) -> None:
            return None

        async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
            return 0

        async def search(
            self,
            *,
            query_vector: Sequence[float],
            tenant_id: str,
            top_k: int = 5,
            score_threshold: float = 0.5,
        ) -> list[ScoredChunk]:
            raise RuntimeError("secret-qdrant-internal-host:6333 unreachable")

        async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
            return None

        async def close(self) -> None:
            return None

    app.dependency_overrides[get_llm_client] = _EmbedReadyLLM
    app.dependency_overrides[get_vector_db] = _FailingSearchVectorDB

    response = await client.post(
        "/api/v1/query/stream",
        json={"query": "Soru", "tenant_id": "tenant_a"},
        headers={"X-Request-ID": "req-pre-stream-503"},
    )
    assert response.status_code == 503
    assert response.headers["x-request-id"] == "req-pre-stream-503"
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert body["error"]["request_id"] == "req-pre-stream-503"
    assert "secret-qdrant" not in response.text


async def test_query_stream_emits_error_when_upstream_sse_truncates_without_done(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """[DONE] veya finish_reason='stop' gelmeden kesilen akışta SSE error olayı dönmelidir."""

    def llm_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/embeddings"):
            return httpx.Response(
                status_code=200,
                json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]},
            )
        return httpx.Response(
            status_code=200,
            text='data: {"choices": [{"delta": {"content": "seq2seq (diziye"}}]}\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(llm_handler))
    llm_service = AsyncOpenAICompatibleLLMClient(
        base_url="http://mock-llm:11434/v1",
        client=mock_http,
    )

    class _StaticVectorDB(BaseVectorDB):
        async def ensure_collection(self) -> None:
            return None

        async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
            return 0

        async def search(
            self,
            *,
            query_vector: Sequence[float],
            tenant_id: str,
            top_k: int = 5,
            score_threshold: float = 0.5,
        ) -> list[ScoredChunk]:
            return [
                ScoredChunk(
                    point_id="pt-1",
                    score=0.9,
                    chunk=DocumentChunk(
                        document_id="doc-1",
                        tenant_id=tenant_id,
                        page_number=2,
                        chunk_index=0,
                        text="RAG combines parametric seq2seq and non-parametric memory.",
                        file_type="pdf",
                    ),
                )
            ]

        async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
            return None

        async def close(self) -> None:
            return None

    app.dependency_overrides[get_llm_client] = lambda: llm_service
    app.dependency_overrides[get_vector_db] = _StaticVectorDB

    try:
        async with client.stream(
            "POST",
            "/api/v1/query/stream",
            json={"query": "Evet, inceleyelim", "tenant_id": "tenant_a"},
            headers={"X-Request-ID": "req-truncated-eof"},
        ) as response:
            assert response.status_code == 200
            raw_bytes = await response.aread()
    finally:
        await mock_http.aclose()

    events = _parse_sse_events(raw_bytes.decode("utf-8"))
    assert [name for name, _ in events] == ["token", "error", "done"]
    assert json.loads(events[0][1]) == {"event": "token", "delta": "seq2seq (diziye"}
    err = json.loads(events[1][1])
    assert err["code"] == "service_unavailable"
    assert "closed before completion" in err["message"]
    assert err["request_id"] == "req-truncated-eof"
    assert events[2] == ("done", "[DONE]")
