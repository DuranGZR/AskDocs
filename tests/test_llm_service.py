import json

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from src.api.dependencies import get_llm_client
from src.core.config import Settings
from src.core.exceptions import ServiceUnavailableError
from src.main import lifespan
from src.services.base import ChatMessage
from src.services.llm_service import (
    AsyncOpenAICompatibleLLMClient,
    SyncOpenAICompatibleLLMClient,
)
from src.services.qdrant_service import AsyncQdrantService


async def test_async_embed_texts_sorts_by_index_and_handles_empty_input() -> None:
    """Asenkron embed_texts boş listede ağa çıkmamalı ve yanıtları index sırasına dizmelidir."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            status_code=200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]},
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1/",
        api_key="test-ollama-key",
        embedding_model="nomic-embed-text",
        client=http_client,
    )

    try:
        empty_result = await service.embed_texts(texts=[])
        assert empty_result == []
        assert len(captured_requests) == 0

        vectors = await service.embed_texts(texts=["birinci metin", "ikinci metin"])
        assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        assert len(captured_requests) == 1

        req = captured_requests[0]
        assert str(req.url) == "http://test-ai:11434/v1/embeddings"
        assert req.headers["Authorization"] == "Bearer test-ollama-key"
        body = json.loads(req.content.decode("utf-8"))
        assert body == {
            "model": "nomic-embed-text",
            "input": ["birinci metin", "ikinci metin"],
        }
    finally:
        await http_client.aclose()


async def test_async_generate_returns_message_content() -> None:
    """generate metodu OpenAI /v1/chat/completions yanıtındaki metni döndürmelidir."""
    captured_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://test-ai:11434/v1/chat/completions"
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            status_code=200,
            json={
                "id": "chatcmpl-123",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "CoreRAG mimarisi izole çalışır.",
                        },
                    }
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        llm_model="llama3.2:3b",
        client=http_client,
    )

    try:
        answer = await service.generate(
            messages=[
                ChatMessage(role="system", content="Sen bir asistansın."),
                ChatMessage(role="user", content="Mimari nasıl çalışır?"),
            ],
            temperature=0.2,
            max_tokens=128,
        )
        assert answer == "CoreRAG mimarisi izole çalışır."
        assert captured_bodies[0] == {
            "model": "llama3.2:3b",
            "messages": [
                {"role": "system", "content": "Sen bir asistansın."},
                {"role": "user", "content": "Mimari nasıl çalışır?"},
            ],
            "temperature": 0.2,
            "max_tokens": 128,
            "stream": False,
        }
    finally:
        await http_client.aclose()


async def test_async_stream_generate_parses_sse_chunks_until_done() -> None:
    """stream_generate SSE satırlarını ayrıştırmalı ve [DONE] işaretinde durmalıdır."""
    sse_body = "\n".join(
        [
            'data: {"choices": [{"delta": {"role": "assistant"}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "Merhaba "}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "CoreRAG!"}}]}',
            "",
            "data: [DONE]",
            'data: {"choices": [{"delta": {"content": "BU_SATIR_OKUNMAMALI"}}]}',
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["stream"] is True
        return httpx.Response(
            status_code=200,
            text=sse_body,
            headers={"Content-Type": "text/event-stream"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        llm_model="llama3.2:3b",
        client=http_client,
    )

    try:
        tokens = [
            token
            async for token in service.stream_generate(
                messages=[ChatMessage(role="user", content="Merhaba")],
                temperature=0.0,
            )
        ]
        assert tokens == ["Merhaba ", "CoreRAG!"]
    finally:
        await http_client.aclose()


def test_sync_embed_texts_works_for_celery_worker() -> None:
    """Celery worker için senkron istemci metinleri vektöre dönüştürmelidir."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://test-ai:11434/v1/embeddings"
        return httpx.Response(
            status_code=200,
            json={
                "data": [
                    {"index": 0, "embedding": [0.7, 0.8]},
                ]
            },
        )

    sync_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = SyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        embedding_model="nomic-embed-text",
        client=sync_http_client,
    )

    try:
        assert service.embed_texts(texts=[]) == []
        vectors = service.embed_texts(texts=["celery chunk"])
        assert vectors == [[0.7, 0.8]]
    finally:
        sync_http_client.close()


def test_sync_embed_texts_retries_on_429_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 429 hız sınırında embed_texts sunucu süresi kadar bekleyip yeniden denemelidir."""
    attempts = 0
    sleep_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                status_code=429,
                json={
                    "error": {
                        "code": 429,
                        "message": "Quota exceeded. Please retry in 2.5s.",
                    }
                },
            )
        return httpx.Response(
            status_code=200,
            json={"data": [{"embedding": [0.9, 1.0]}]},
        )

    monkeypatch.setattr("src.services.llm_service.time.sleep", sleep_calls.append)

    sync_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = SyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        embedding_model="gemini-embedding-001",
        client=sync_http_client,
    )

    try:
        vectors = service.embed_texts(texts=["retry chunk"])
        assert attempts == 2
        assert sleep_calls == [4.0]
        assert vectors == [[0.9, 1.0]]
    finally:
        sync_http_client.close()


async def test_llm_service_raises_service_unavailable_on_upstream_error() -> None:
    """AI sunucusu 5xx döndürdüğünde ServiceUnavailableError fırlatılmalıdır."""

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=503, text="Model overloaded")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        client=http_client,
    )

    try:
        with pytest.raises(ServiceUnavailableError) as exc_info:
            await service.generate(messages=[ChatMessage(role="user", content="Test")])
        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "service_unavailable"
    finally:
        await http_client.aclose()


async def test_lifespan_initializes_and_closes_llm_client(
    app: FastAPI,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uygulama lifespan'i LLM istemcisini hazırlamalı ve kapanışta kapatmalıdır."""
    events: list[str] = []

    class _FakeAsyncQdrantService(AsyncQdrantService):
        def __init__(self, **kwargs: object) -> None:
            pass

        async def ensure_collection(self) -> None:
            pass

        async def close(self) -> None:
            events.append("qdrant_close")

    class _FakeAsyncLLMClient(AsyncOpenAICompatibleLLMClient):
        def __init__(self, **kwargs: object) -> None:
            events.append("llm_init")

        async def close(self) -> None:
            events.append("llm_close")

    monkeypatch.setattr("src.main.AsyncQdrantService", _FakeAsyncQdrantService)
    monkeypatch.setattr("src.main.AsyncOpenAICompatibleLLMClient", _FakeAsyncLLMClient)

    async with lifespan(app):
        assert events == ["llm_init"]
        scope = {"type": "http", "app": app, "headers": []}
        req = Request(scope)
        resolved = get_llm_client(req, test_settings)
        assert resolved is app.state.llm_client

    assert events == ["llm_init", "llm_close", "qdrant_close"]
