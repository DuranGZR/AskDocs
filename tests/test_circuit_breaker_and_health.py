from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from redis.exceptions import ConnectionError as RedisConnectionError

from src.api.dependencies import (
    get_health_checker,
    get_llm_circuit_breaker,
    get_llm_client,
    get_qdrant_circuit_breaker,
    get_settings,
    get_vector_db,
)
from src.core.circuit_breaker import CircuitBreaker, SyncCircuitBreaker
from src.core.config import Settings
from src.core.exceptions import (
    BadRequestError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from src.services.base import (
    BaseVectorDB,
    ChatMessage,
    IndexedDocumentSummary,
    ScoredChunk,
    VectorPoint,
)
from src.services.health_service import DependencyHealthChecker
from src.services.llm_service import (
    AsyncOpenAICompatibleLLMClient,
    SyncOpenAICompatibleLLMClient,
)
from src.services.qdrant_service import AsyncQdrantService, SyncQdrantService
from src.services.rate_limiter import RedisRateLimiter


async def test_circuit_breaker_transitions_closed_open_half_open_and_recovered() -> None:
    """Devre kesici ardışık hatalarda açılmalı, cooldown sonra probe ile kapanmalıdır."""
    now = 100.0
    breaker = CircuitBreaker(
        name="test-svc",
        failure_threshold=3,
        recovery_timeout_seconds=30.0,
        half_open_max_calls=1,
        half_open_success_threshold=2,
        clock=lambda: now,
    )
    upstream_calls = 0

    async def fail_op() -> str:
        nonlocal upstream_calls
        upstream_calls += 1
        raise ServiceUnavailableError("upstream down")

    async def ok_op() -> str:
        nonlocal upstream_calls
        upstream_calls += 1
        return "ok"

    # 2 hata sonrası gelen başarı sayacı sıfırlamalıdır
    for _ in range(2):
        with pytest.raises(ServiceUnavailableError):
            await breaker.call(fail_op)
    assert breaker.state == "closed"
    assert breaker.failure_count == 2
    assert await breaker.call(ok_op) == "ok"
    assert breaker.failure_count == 0

    # 3 ardışık hata devreyi OPEN konumuna alır
    for _ in range(3):
        with pytest.raises(ServiceUnavailableError):
            await breaker.call(fail_op)
    assert breaker.state == "open"
    calls_when_opened = upstream_calls

    # Bekleme süresi dolmadan çağrılar yukarı akışa çıkmadan 503 ile reddedilir
    now = 112.0
    with pytest.raises(ServiceUnavailableError) as open_exc:
        await breaker.call(ok_op)
    assert upstream_calls == calls_when_opened
    assert open_exc.value.headers is not None
    assert open_exc.value.headers["Retry-After"] == "18"

    # Cooldown dolunca HALF_OPEN probe başarısız olursa devre yeniden OPEN olur
    now = 131.0
    with pytest.raises(ServiceUnavailableError):
        await breaker.call(fail_op)
    assert breaker.state == "open"
    assert upstream_calls == calls_when_opened + 1

    # Yeni cooldown sonrası 2 ardışık başarılı probe ile CLOSED durumuna döner
    now = 162.0
    assert await breaker.call(ok_op) == "ok"
    assert breaker.state == "half_open"
    assert await breaker.call(ok_op) == "ok"
    assert breaker.state == "closed"
    assert breaker.failure_count == 0


async def test_circuit_breaker_ignores_client_errors_cancellation_and_early_stream_close() -> None:
    """4xx istemci hataları, CancelledError ve erken akış kapatma devreyi açmamalıdır."""
    now = 50.0
    breaker = CircuitBreaker(
        name="stream-svc",
        failure_threshold=2,
        recovery_timeout_seconds=10.0,
        half_open_max_calls=1,
        clock=lambda: now,
    )

    async def client_err(exc: Exception) -> None:
        raise exc

    for exc in (BadRequestError("bad"), UnauthorizedError("no"), NotFoundError("missing")):
        with pytest.raises(type(exc)):
            await breaker.call(client_err, exc)
    assert breaker.state == "closed"
    assert breaker.failure_count == 0

    # İptal edilen görev hata yazmamalı, HALF_OPEN slotu da sızdırmamalıdır
    async def trip() -> None:
        raise ServiceUnavailableError("down")

    for _ in range(2):
        with pytest.raises(ServiceUnavailableError):
            await breaker.call(trip)
    assert breaker.state == "open"

    now = 65.0
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow_probe() -> str:
        started.set()
        await gate.wait()
        return "done"

    probe_task = asyncio.create_task(breaker.call(slow_probe))
    await started.wait()
    probe_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe_task

    # İptal sonrası HALF_OPEN slotu boşalmış olmalı; yeni probe akışı başlayıp erken kapatılabilmeli
    async def gen_tokens() -> AsyncIterator[str]:
        yield "ilk"
        yield "ikinci"

    stream_iter = breaker.stream(gen_tokens)
    first = await anext(stream_iter)
    assert first == "ilk"
    await stream_iter.aclose()

    # Erken aclose devreyi açmamalıdır; tam başarı CLOSED konumuna döndürmelidir
    assert breaker.state == "half_open"
    gate.set()
    assert await breaker.call(slow_probe) == "done"
    assert breaker.state == "closed"


async def test_circuit_breaker_half_open_blocks_concurrent_thundering_herd() -> None:
    """HALF_OPEN aşamasında aynı anda yalnızca izin verilen sayıda probe çağrısı geçmelidir."""
    now = 0.0
    breaker = CircuitBreaker(
        name="herd-guard",
        failure_threshold=1,
        recovery_timeout_seconds=5.0,
        half_open_max_calls=1,
        clock=lambda: now,
    )

    async def fail_once() -> None:
        raise ServiceUnavailableError("down")

    with pytest.raises(ServiceUnavailableError):
        await breaker.call(fail_once)
    assert breaker.state == "open"

    now = 6.0
    probe_entered = asyncio.Event()
    release_probe = asyncio.Event()

    async def slow_recover() -> str:
        probe_entered.set()
        await release_probe.wait()
        return "healthy"

    first_task = asyncio.create_task(breaker.call(slow_recover))
    await probe_entered.wait()
    assert breaker.state == "half_open"

    # İkinci eşzamanlı çağrı probe devam ederken 503 ile engellenmelidir
    with pytest.raises(ServiceUnavailableError) as concurrent_exc:
        await breaker.call(slow_recover)
    assert concurrent_exc.value.headers is not None
    assert concurrent_exc.value.headers["Retry-After"] == "1"

    release_probe.set()
    assert await first_task == "healthy"
    assert breaker.state == "closed"


async def test_qdrant_and_llm_services_fast_fail_when_circuit_opens() -> None:
    """Async/Sync Qdrant ve LLM istemcileri açık devrede ağ çağrısı yapmadan 503 dönmelidir."""
    # 1. Async LLM + SSE akışı
    llm_breaker = CircuitBreaker(
        name="llm",
        failure_threshold=2,
        recovery_timeout_seconds=30.0,
    )
    http_attempts = 0

    def llm_handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_attempts
        http_attempts += 1
        return httpx.Response(status_code=503, text="model unavailable")

    async_http = httpx.AsyncClient(transport=httpx.MockTransport(llm_handler))
    llm_client = AsyncOpenAICompatibleLLMClient(
        base_url="http://llm-test:11434/v1",
        client=async_http,
        circuit_breaker=llm_breaker,
    )
    try:
        for _ in range(2):
            with pytest.raises(ServiceUnavailableError):
                await llm_client.generate(messages=[ChatMessage(role="user", content="soru")])
        assert llm_breaker.state == "open"
        assert http_attempts == 2

        with pytest.raises(ServiceUnavailableError):
            await llm_client.embed_texts(texts=["deneme"])
        with pytest.raises(ServiceUnavailableError):
            async for _ in llm_client.stream_generate(
                messages=[ChatMessage(role="user", content="soru")]
            ):
                pass
        assert http_attempts == 2
    finally:
        await async_http.aclose()

    # 2. Async Qdrant + raw ping bypass
    qdrant_breaker = CircuitBreaker(
        name="qdrant",
        failure_threshold=2,
        recovery_timeout_seconds=30.0,
    )
    qdrant_calls = 0

    class _FailingQdrantClient:
        async def query_points(self, **kwargs: Any) -> Any:
            nonlocal qdrant_calls
            qdrant_calls += 1
            raise RuntimeError("qdrant socket refused")

        async def get_collections(self, **kwargs: Any) -> Any:
            return []

    vdb = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=cast(AsyncQdrantClient, _FailingQdrantClient()),
        circuit_breaker=qdrant_breaker,
    )
    for _ in range(2):
        with pytest.raises(ServiceUnavailableError):
            await vdb.search(query_vector=[1.0, 0.0, 0.0, 0.0], tenant_id="t1")
    assert qdrant_breaker.state == "open"
    assert qdrant_calls == 2

    with pytest.raises(ServiceUnavailableError):
        await vdb.search(query_vector=[1.0, 0.0, 0.0, 0.0], tenant_id="t1")
    assert qdrant_calls == 2
    assert await vdb.ping() is True

    # 3. Sync Worker Qdrant & LLM devre kesicileri
    sync_llm_breaker = SyncCircuitBreaker(name="worker.llm", failure_threshold=1)
    sync_http_attempts = 0

    def sync_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sync_http_attempts
        sync_http_attempts += 1
        return httpx.Response(status_code=503, text="down")

    sync_http = httpx.Client(transport=httpx.MockTransport(sync_handler))
    sync_llm = SyncOpenAICompatibleLLMClient(
        base_url="http://llm-test:11434/v1",
        client=sync_http,
        circuit_breaker=sync_llm_breaker,
    )
    try:
        with pytest.raises(ServiceUnavailableError):
            sync_llm.embed_texts(texts=["parça"])
        assert sync_llm_breaker.state == "open"
        with pytest.raises(ServiceUnavailableError):
            sync_llm.embed_texts(texts=["ikinci"])
        assert sync_http_attempts == 1
    finally:
        sync_http.close()

    sync_qdrant_breaker = SyncCircuitBreaker(name="worker.qdrant", failure_threshold=1)

    class _FailingSyncQdrant:
        def upsert(self, **kwargs: Any) -> None:
            raise RuntimeError("write refused")

    sync_vdb = SyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=_FailingSyncQdrant(),  # type: ignore[arg-type]
        circuit_breaker=sync_qdrant_breaker,
    )
    sample_pt = VectorPoint(
        point_id="1",
        vector=[1.0, 0.0, 0.0, 0.0],
        chunk=IndexedChunkHelper.make(),
    )
    with pytest.raises(ServiceUnavailableError):
        sync_vdb.upsert(points=[sample_pt])
    assert sync_qdrant_breaker.state == "open"


class IndexedChunkHelper:
    """Test içi tekil VectorPoint kurulumunu sade tutar."""

    @staticmethod
    def make() -> Any:
        from src.services.base import DocumentChunk

        return DocumentChunk(
            document_id="d1",
            tenant_id="t1",
            page_number=1,
            chunk_index=0,
            text="örnek",
            file_type="pdf",
        )


async def test_api_endpoints_expose_open_circuit_and_fast_fail_503(
    app: FastAPI,
    client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """API uçları ardışık Qdrant/LLM hatasında 503 Retry-After dönmeli ve /health'e yansımalıdır."""
    cb_settings = test_settings.model_copy(
        update={
            "CIRCUIT_BREAKER_ENABLED": True,
            "CIRCUIT_BREAKER_FAILURE_THRESHOLD": 2,
            "CIRCUIT_BREAKER_RECOVERY_SECONDS": 30.0,
        }
    )
    app.dependency_overrides[get_settings] = lambda: cb_settings

    qdrant_attempts = 0
    qdrant_breaker = get_qdrant_circuit_breaker(
        type("_Req", (), {"app": app})(),  # type: ignore[arg-type]
        cb_settings,
    )
    llm_breaker = get_llm_circuit_breaker(
        type("_Req", (), {"app": app})(),  # type: ignore[arg-type]
        cb_settings,
    )

    class _BrokenAsyncQdrantClient:
        async def scroll(self, **kwargs: Any) -> Any:
            nonlocal qdrant_attempts
            qdrant_attempts += 1
            raise RuntimeError("qdrant unavailable")

        async def get_collections(self, **kwargs: Any) -> Any:
            return []

    vdb = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=cast(AsyncQdrantClient, _BrokenAsyncQdrantClient()),
        circuit_breaker=qdrant_breaker,
    )
    llm_attempts = 0

    def llm_err_handler(request: httpx.Request) -> httpx.Response:
        nonlocal llm_attempts
        llm_attempts += 1
        return httpx.Response(status_code=503, text="ollama down")

    async_http = httpx.AsyncClient(transport=httpx.MockTransport(llm_err_handler))
    llm_client = AsyncOpenAICompatibleLLMClient(
        base_url="http://llm-test:11434/v1",
        client=async_http,
        circuit_breaker=llm_breaker,
    )
    app.dependency_overrides[get_vector_db] = lambda: vdb
    app.dependency_overrides[get_llm_client] = lambda: llm_client

    try:
        for _ in range(2):
            resp = await client.get("/api/v1/documents", params={"tenant_id": "tenant_alpha"})
            assert resp.status_code == 503
        assert qdrant_attempts == 2

        fast_qdrant = await client.get("/api/v1/documents", params={"tenant_id": "tenant_alpha"})
        assert fast_qdrant.status_code == 503
        assert int(fast_qdrant.headers["retry-after"]) >= 1
        assert qdrant_attempts == 2

        for _ in range(2):
            q_resp = await client.post(
                "/api/v1/query",
                json={"query": "Soru?", "tenant_id": "tenant_alpha", "stream": False},
            )
            assert q_resp.status_code == 503
        assert llm_attempts == 2

        fast_llm = await client.post(
            "/api/v1/query",
            json={"query": "Tekrar?", "tenant_id": "tenant_alpha", "stream": False},
        )
        assert fast_llm.status_code == 503
        assert int(fast_llm.headers["retry-after"]) >= 1
        assert llm_attempts == 2

        health_resp = await client.get("/api/v1/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["circuits"] == {"qdrant": "open", "llm": "open"}
    finally:
        await async_http.aclose()


async def test_dependency_health_checker_redis_qdrant_and_timeouts(
    app: FastAPI,
    client: httpx.AsyncClient,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """/api/v1/health Redis ve Qdrant ping hatalarında veya zaman aşımında 503 dönmelidir."""

    class _ConfigurableVectorDB(BaseVectorDB):
        def __init__(self, *, ping_ok: bool = True, delay: float = 0.0) -> None:
            self._ping_ok = ping_ok
            self._delay = delay

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
            return []

        async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
            return None

        async def list_documents(self, *, tenant_id: str) -> list[IndexedDocumentSummary]:
            return []

        async def list_tenants(self) -> list[str]:
            return []

        async def ping(self, *, timeout_seconds: float = 2.0) -> bool:
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            return self._ping_ok

        async def close(self) -> None:
            return None

    async def _redis_ok(_url: str, _timeout: float) -> bool:
        return True

    async def _redis_down(_url: str, _timeout: float) -> bool:
        raise RedisConnectionError("redis connection refused")

    # 1. Tümü sağlıklı -> 200 OK
    app.dependency_overrides[get_health_checker] = lambda: DependencyHealthChecker(
        upload_dir=test_settings.UPLOAD_DIR,
        redis_url=test_settings.REDIS_URL,
        vector_db=_ConfigurableVectorDB(ping_ok=True),
        timeout_seconds=0.2,
        redis_probe=_redis_ok,
    )
    ok_resp = await client.get("/api/v1/health")
    assert ok_resp.status_code == 200
    assert ok_resp.json()["checks"] == {"storage": "ok", "redis": "ok", "qdrant": "ok"}

    # 2. Redis kapalı -> 503 degraded
    app.dependency_overrides[get_health_checker] = lambda: DependencyHealthChecker(
        upload_dir=test_settings.UPLOAD_DIR,
        redis_url=test_settings.REDIS_URL,
        vector_db=_ConfigurableVectorDB(ping_ok=True),
        timeout_seconds=0.2,
        redis_probe=_redis_down,
    )
    redis_err_resp = await client.get("/api/v1/health")
    assert redis_err_resp.status_code == 503
    assert redis_err_resp.json()["status"] == "degraded"
    assert redis_err_resp.json()["checks"] == {
        "storage": "ok",
        "redis": "unavailable",
        "qdrant": "ok",
    }

    # 3. Qdrant zaman aşımı -> 503 degraded
    app.dependency_overrides[get_health_checker] = lambda: DependencyHealthChecker(
        upload_dir=test_settings.UPLOAD_DIR,
        redis_url=test_settings.REDIS_URL,
        vector_db=_ConfigurableVectorDB(ping_ok=True, delay=0.5),
        timeout_seconds=0.05,
        redis_probe=_redis_ok,
    )
    qdrant_timeout_resp = await client.get("/api/v1/health")
    assert qdrant_timeout_resp.status_code == 503
    assert qdrant_timeout_resp.json()["checks"] == {
        "storage": "ok",
        "redis": "ok",
        "qdrant": "unavailable",
    }

    # 4. Gerçek AsyncQdrantService.ping ve RedisRateLimiter.ping davranışı
    memory_vdb = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=AsyncQdrantClient(location=":memory:"),
    )
    assert await memory_vdb.ping() is True
    await memory_vdb.close()

    class _PingRedis:
        def __init__(self, should_fail: bool = False) -> None:
            self._should_fail = should_fail

        def register_script(self, script: str) -> Any:
            return None

        async def ping(self) -> bool:
            if self._should_fail:
                raise RedisConnectionError("redis offline")
            return True

        async def aclose(self) -> None:
            return None

    rl_ok = RedisRateLimiter(client=_PingRedis(should_fail=False))
    rl_fail = RedisRateLimiter(client=_PingRedis(should_fail=True))
    assert await rl_ok.ping() is True
    assert await rl_fail.ping() is False
