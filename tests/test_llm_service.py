import json

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from src.api.dependencies import get_llm_client
from src.core.circuit_breaker import CircuitBreaker
from src.core.config import Settings
from src.core.exceptions import AppError, ServiceUnavailableError
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


async def test_async_generate_retries_short_429_without_tripping_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kısa 429 hız sınırında generate yeniden denemeli ve devre kesiciyi açmamalıdır."""
    from src.core.circuit_breaker import CircuitBreaker

    attempts = 0
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("src.services.llm_service.asyncio.sleep", _fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                status_code=429,
                headers={"Retry-After": "2"},
                text="rate limited",
            )
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"role": "assistant", "content": "Kurtarıldı"}}]},
        )

    breaker = CircuitBreaker(name="llm", failure_threshold=2)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        client=http_client,
        circuit_breaker=breaker,
    )

    try:
        answer = await service.generate(messages=[ChatMessage(role="user", content="Soru")])
        assert answer == "Kurtarıldı"
        assert attempts == 2
        assert sleep_calls == [3.0]
        assert breaker.state == "closed"
        assert breaker.failure_count == 0
    finally:
        await http_client.aclose()


async def test_async_stream_generate_429_retry_and_budget_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_generate kısa 429'da kurtarmalı, uzun beklemede hızlı 503 Retry-After dönmelidir."""
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("src.services.llm_service.asyncio.sleep", _fake_sleep)

    attempts = 0

    def recovering_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code=429, headers={"Retry-After": "1"}, text="busy")
        return httpx.Response(
            status_code=200,
            text='data: {"choices": [{"delta": {"content": "Akış OK"}}]}\n\ndata: [DONE]\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    http_ok = httpx.AsyncClient(transport=httpx.MockTransport(recovering_handler))
    svc_ok = AsyncOpenAICompatibleLLMClient(base_url="http://test-ai:11434/v1", client=http_ok)
    try:
        tokens = [
            t
            async for t in svc_ok.stream_generate(
                messages=[ChatMessage(role="user", content="Test")]
            )
        ]
        assert tokens == ["Akış OK"]
        assert attempts == 2
        assert sleep_calls == [2.0]
    finally:
        await http_ok.aclose()

    sleep_calls.clear()

    def long_wait_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429, headers={"Retry-After": "45"}, text="quota exhausted"
        )

    http_long = httpx.AsyncClient(transport=httpx.MockTransport(long_wait_handler))
    svc_long = AsyncOpenAICompatibleLLMClient(base_url="http://test-ai:11434/v1", client=http_long)
    try:
        with pytest.raises(ServiceUnavailableError) as exc_info:
            async for _ in svc_long.stream_generate(
                messages=[ChatMessage(role="user", content="Test")]
            ):
                pass
        assert sleep_calls == []
        assert exc_info.value.headers == {"Retry-After": "46"}
    finally:
        await http_long.aclose()


async def test_async_stream_generate_rejects_premature_eof_without_done_or_stop() -> None:
    """[DONE] veya finish_reason='stop' gelmeden kesilen SSE akışı hata yükseltmelidir."""
    partial_sse = 'data: {"choices": [{"delta": {"content": "Yarım kalan "}}]}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            text=partial_sse,
            headers={"Content-Type": "text/event-stream"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        client=http_client,
    )
    collected: list[str] = []
    try:
        with pytest.raises(ServiceUnavailableError, match="closed before completion"):
            async for token in service.stream_generate(
                messages=[ChatMessage(role="user", content="Soru")]
            ):
                collected.append(token)
        assert collected == ["Yarım kalan "]
    finally:
        await http_client.aclose()


async def test_async_stream_generate_handles_finish_reasons_and_sse_error_frames() -> None:
    """stop dışındaki finish_reason değerleri ve SSE error çerçeveleri akışı hata ile kesmelidir."""
    # 1. Yalnızca finish_reason='stop' ile temiz kapanış ([DONE] olmadan)
    stop_only_sse = "\n".join(
        [
            'data: {"choices": [{"delta": {"content": "Tam yanıt"}}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
            "",
        ]
    )
    http_stop = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                status_code=200,
                text=stop_only_sse,
                headers={"Content-Type": "text/event-stream"},
            )
        )
    )
    svc_stop = AsyncOpenAICompatibleLLMClient(base_url="http://test-ai:11434/v1", client=http_stop)
    try:
        tokens = [
            t
            async for t in svc_stop.stream_generate(
                messages=[ChatMessage(role="user", content="Soru")]
            )
        ]
        assert tokens == ["Tam yanıt"]
    finally:
        await http_stop.aclose()

    # 2. content_filter / length akışı erken sonlandırır ancak devre kesiciyi açmaz
    breaker = CircuitBreaker(name="llm-finish-reason", failure_threshold=1)
    filtered_sse = "\n".join(
        [
            'data: {"choices": [{"delta": {"content": "İlk parça"}}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "content_filter"}]}',
            "data: [DONE]",
        ]
    )
    http_filtered = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                status_code=200,
                text=filtered_sse,
                headers={"Content-Type": "text/event-stream"},
            )
        )
    )
    svc_filtered = AsyncOpenAICompatibleLLMClient(
        base_url="http://test-ai:11434/v1",
        client=http_filtered,
        circuit_breaker=breaker,
    )
    filtered_tokens: list[str] = []
    try:
        with pytest.raises(AppError) as exc_info:
            async for t in svc_filtered.stream_generate(
                messages=[ChatMessage(role="user", content="Soru")]
            ):
                filtered_tokens.append(t)
        assert filtered_tokens == ["İlk parça"]
        assert exc_info.value.code == "content_filtered"
        assert exc_info.value.status_code == 422
        assert breaker.state == "closed"
        assert breaker.failure_count == 0
    finally:
        await http_filtered.aclose()

    # 3. SSE veri satırı içindeki yukarı akış error nesnesi 503 döndürmelidir
    error_sse = "\n".join(
        [
            'data: {"choices": [{"delta": {"content": "Parça"}}]}',
            'data: {"error": {"message": "stream aborted upstream", "type": "server_error"}}',
        ]
    )
    http_err = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                status_code=200,
                text=error_sse,
                headers={"Content-Type": "text/event-stream"},
            )
        )
    )
    svc_err = AsyncOpenAICompatibleLLMClient(base_url="http://test-ai:11434/v1", client=http_err)
    try:
        with pytest.raises(ServiceUnavailableError, match="reported an error during streaming"):
            async for _ in svc_err.stream_generate(
                messages=[ChatMessage(role="user", content="Soru")]
            ):
                pass
    finally:
        await http_err.aclose()


def test_provider_label_inference_reset_headers_and_rate_limit_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sağlayıcı etiketi, reset başlıkları (1m10s/12.5s) ve 429 geri çağrımı çalışmalıdır."""
    from src.services.llm_service import _extract_retry_delay_seconds, infer_provider_label

    assert infer_provider_label("https://generativelanguage.googleapis.com/v1beta/openai") == (
        "Google Gemini API"
    )
    assert infer_provider_label("https://api.openai.com/v1") == "OpenAI API"
    assert infer_provider_label("https://api.groq.com/openai/v1") == "Groq API"
    assert infer_provider_label("http://localhost:11434/v1") == "Yerel AI Sunucusu"
    assert infer_provider_label("http://tei:80/v1") == "Yerel AI Sunucusu"
    assert infer_provider_label("http://text-embeddings-inference:80/v1") == "Yerel AI Sunucusu"
    assert infer_provider_label("https://custom.llm.example.com/v1") == (
        "AI API (custom.llm.example.com)"
    )

    groq_429 = httpx.Response(
        status_code=429,
        headers={"x-ratelimit-reset-requests": "12.5s"},
        text="rate limit reached",
    )
    assert _extract_retry_delay_seconds(groq_429, 0) == pytest.approx(13.5)

    minute_429 = httpx.Response(
        status_code=429,
        headers={"x-ratelimit-reset-tokens": "1m10s"},
        text="tokens exhausted",
    )
    assert _extract_retry_delay_seconds(minute_429, 0) == pytest.approx(65.0)

    recorded_callbacks: list[tuple[float, int, int, str]] = []
    sleep_calls: list[float] = []
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                status_code=429,
                headers={"x-ratelimit-reset-requests": "4s"},
                text="busy",
            )
        return httpx.Response(status_code=200, json={"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("src.services.llm_service.time.sleep", sleep_calls.append)
    sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = SyncOpenAICompatibleLLMClient(
        base_url="https://api.groq.com/openai/v1",
        embedding_model="text-embedding-3-small",
        client=sync_client,
    )
    service.set_rate_limit_callback(
        lambda delay, attempt, max_retries, provider: recorded_callbacks.append(
            (delay, attempt, max_retries, provider)
        )
    )
    try:
        vectors = service.embed_texts(texts=["hello"])
        assert vectors == [[0.1, 0.2]]
        assert sleep_calls == [5.0]
        assert recorded_callbacks == [(5.0, 1, 8, "Groq API")]
    finally:
        sync_client.close()


def test_daily_quota_429_aborts_immediately_without_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Günlük kota (per day / 15h22m) dolduğunda beklemeden QuotaExhaustedError fırlatmalıdır."""
    from src.core.circuit_breaker import SyncCircuitBreaker
    from src.services.llm_service import QuotaExhaustedError

    sleep_calls: list[float] = []
    callback_calls: list[object] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            json={
                "error": {
                    "code": 429,
                    "message": (
                        "You exceeded your current quota.\n"
                        "* Quota exceeded for metric: "
                        "generativelanguage.googleapis.com/embed_content_free_tier_requests, "
                        "limit: 1000, model: gemini-embedding-1.0, description: "
                        "Embed content requests per day limit exceeded on Free Tier.\n"
                        "Please retry in 15h22m46.913669153s."
                    ),
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
        )

    monkeypatch.setattr("src.services.llm_service.time.sleep", sleep_calls.append)
    breaker = SyncCircuitBreaker(name="worker.llm", failure_threshold=1)
    sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    service = SyncOpenAICompatibleLLMClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        embedding_model="gemini-embedding-001",
        client=sync_client,
        circuit_breaker=breaker,
    )
    service.set_rate_limit_callback(lambda *args: callback_calls.append(args))
    try:
        with pytest.raises(QuotaExhaustedError) as exc_info:
            service.embed_texts(texts=["daily quota test"])
        assert exc_info.value.status_code == 429
        assert exc_info.value.code == "daily_quota_exhausted"
        assert (
            str(exc_info.value)
            == "❌ Google Gemini API günlük ücretsiz kotası (1.000 parça/gün) doldu."
        )
        assert sleep_calls == []
        assert callback_calls == []
        assert breaker.state == "closed"
    finally:
        sync_client.close()


async def test_async_hybrid_client_routes_embeddings_and_chat_separately() -> None:
    """Hibrit istemci embedding'i yerel TEI'ye, chat çağrılarını bulut LLM'e yönlendirmelidir."""
    captured_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(
            (str(request.url), request.headers.get("Authorization", ""), request.content.decode())
        )
        if str(request.url).endswith("/embeddings"):
            return httpx.Response(
                status_code=200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
            )
        return httpx.Response(
            status_code=200,
            json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "Yanıt"}}]},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AsyncOpenAICompatibleLLMClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="cloud-secret",
        llm_model="gemini-3.5-flash-lite",
        embedding_model="BAAI/bge-m3",
        embedding_base_url="http://tei:80/v1/",
        embedding_api_key="local-tei",
        client=http_client,
    )
    try:
        vectors = await service.embed_texts(texts=["merhaba"])
        answer = await service.generate(messages=[ChatMessage(role="user", content="Soru")])

        assert vectors == [[0.1, 0.2, 0.3]]
        assert answer == "Yanıt"
        assert len(captured_requests) == 2

        emb_url, emb_auth, emb_body = captured_requests[0]
        assert emb_url == "http://tei/v1/embeddings"
        assert emb_auth == "Bearer local-tei"
        assert json.loads(emb_body)["model"] == "BAAI/bge-m3"

        chat_url, chat_auth, chat_body = captured_requests[1]
        assert (
            chat_url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        assert chat_auth == "Bearer cloud-secret"
        assert json.loads(chat_body)["model"] == "gemini-3.5-flash-lite"
    finally:
        await http_client.aclose()
