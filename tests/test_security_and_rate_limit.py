from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError

from src.api.dependencies import get_llm_client, get_rate_limiter, get_settings, get_vector_db
from src.core.config import Settings
from src.core.exceptions import ServiceUnavailableError
from src.services.base import (
    BaseLLMClient,
    BaseVectorDB,
    ChatMessage,
    IndexedDocumentSummary,
    ScoredChunk,
    VectorPoint,
)
from src.services.rate_limiter import (
    InMemoryRateLimiter,
    RedisRateLimiter,
    build_rate_limit_subject,
)


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
        return []

    async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        return None

    async def list_documents(self, *, tenant_id: str) -> list[IndexedDocumentSummary]:
        return []

    async def list_tenants(self) -> list[str]:
        return ["test-user"]

    async def close(self) -> None:
        return None


class _StubLLMClient(BaseLLMClient):
    async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "Test cevabı"

    async def stream_generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "Test "
        yield "akışı"

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        (
            "POST",
            "/api/v1/documents/upload",
            {
                "data": {"tenant_id": "acme_corp"},
                "files": {"file": ("notes.txt", b"hello", "text/plain")},
            },
        ),
        ("GET", "/api/v1/documents", {"params": {"tenant_id": "acme_corp"}}),
        ("GET", "/api/v1/documents/tenants", {}),
        ("GET", "/api/v1/documents/status/task-123", {}),
        ("DELETE", "/api/v1/documents/doc-123", {"params": {"tenant_id": "acme_corp"}}),
        ("POST", "/api/v1/query", {"json": {"query": "test sorusu", "tenant_id": "acme_corp"}}),
        (
            "POST",
            "/api/v1/query/stream",
            {"json": {"query": "test sorusu", "tenant_id": "acme_corp", "stream": True}},
        ),
    ],
)
async def test_protected_endpoints_reject_missing_api_key_with_401(
    unauthenticated_client: httpx.AsyncClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    """Tüm doküman ve sorgu uç noktaları anahtar yokken 401 Unauthorized dönmelidir."""
    response = await unauthenticated_client.request(method, path, **kwargs)

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "ApiKey"
    assert "x-request-id" in response.headers
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["request_id"] == response.headers["x-request-id"]


async def test_api_key_validation_variants_and_secret_hygiene(
    app: FastAPI,
    unauthenticated_client: httpx.AsyncClient,
    test_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Yanlış/boş anahtar 401 dönmeli, geçerli anahtar kabul edilmeli ve sır sızmamalıdır."""
    app.dependency_overrides[get_vector_db] = lambda: _StubVectorDB()
    valid_key = test_settings.API_KEY.get_secret_value()
    wrong_key = "wrong-secret-candidate-999"

    with caplog.at_level(logging.WARNING):
        bad_header_resp = await unauthenticated_client.get(
            "/api/v1/documents/tenants",
            headers={"X-API-Key": wrong_key},
        )
        assert bad_header_resp.status_code == 401

        whitespace_resp = await unauthenticated_client.get(
            "/api/v1/documents/tenants",
            headers={"X-API-Key": "    "},
        )
        assert whitespace_resp.status_code == 401

        bad_bearer_resp = await unauthenticated_client.get(
            "/api/v1/documents/tenants",
            headers={"Authorization": f"Bearer {wrong_key}"},
        )
        assert bad_bearer_resp.status_code == 401

        # X-API-Key açıkça yanlış verildiyse geçerli Bearer olsa bile reddedilmelidir
        conflict_resp = await unauthenticated_client.get(
            "/api/v1/documents/tenants",
            headers={"X-API-Key": wrong_key, "Authorization": f"Bearer {valid_key}"},
        )
        assert conflict_resp.status_code == 401

    assert valid_key not in caplog.text
    assert wrong_key not in caplog.text
    assert valid_key not in bad_header_resp.text
    assert wrong_key not in bad_header_resp.text

    ok_x_api_key = await unauthenticated_client.get(
        "/api/v1/documents/tenants",
        headers={"X-API-Key": f"  {valid_key}  "},
    )
    assert ok_x_api_key.status_code == 200

    ok_bearer = await unauthenticated_client.get(
        "/api/v1/documents/tenants",
        headers={"Authorization": f"Bearer {valid_key}"},
    )
    assert ok_bearer.status_code == 200


async def test_public_health_and_ui_routes_remain_accessible_without_api_key(
    unauthenticated_client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Sağlık kontrolü ve Web UI statik varlıkları API anahtarı olmadan açılmalıdır."""
    health_resp = await unauthenticated_client.get("/api/v1/health")
    assert health_resp.status_code == 200

    html_resp = await unauthenticated_client.get("/")
    assert html_resp.status_code == 200
    assert 'id="api-key-modal"' in html_resp.text

    js_resp = await unauthenticated_client.get("/static/app.js")
    assert js_resp.status_code == 200
    assert "X-API-Key" in js_resp.text
    assert test_settings.API_KEY.get_secret_value() not in html_resp.text
    assert test_settings.API_KEY.get_secret_value() not in js_resp.text


async def test_in_memory_rate_limiter_sliding_window_and_lockout_stability() -> None:
    """Kayan pencere limiti korumalı, reddedilen istekler kilidi uzatmamalıdır."""
    now = 1000.0
    limiter = InMemoryRateLimiter(clock=lambda: now)

    d1 = await limiter.check_and_consume(scope="upload", subject="s1", limit=2, window_seconds=10)
    d2 = await limiter.check_and_consume(scope="upload", subject="s1", limit=2, window_seconds=10)
    assert (d1.allowed, d1.remaining) == (True, 1)
    assert (d2.allowed, d2.remaining) == (True, 0)

    # T=1004 iken gelen reddedilmiş isteklerin ilk kaydın 1010'daki çıkışını geciktirmemesi gerekir
    now = 1004.0
    d3 = await limiter.check_and_consume(scope="upload", subject="s1", limit=2, window_seconds=10)
    assert d3.allowed is False
    assert d3.retry_after_seconds == 6

    # Farklı kapsam veya özne aynı anda etkilenmemelidir
    other_scope = await limiter.check_and_consume(
        scope="query", subject="s1", limit=2, window_seconds=10
    )
    other_subject = await limiter.check_and_consume(
        scope="upload", subject="s2", limit=2, window_seconds=10
    )
    assert other_scope.allowed is True
    assert other_subject.allowed is True

    # Pencere dolunca (T=1010.5) ilk slot boşalmalıdır
    now = 1010.5
    d4 = await limiter.check_and_consume(scope="upload", subject="s1", limit=2, window_seconds=10)
    assert d4.allowed is True


async def test_redis_rate_limiter_lua_contract_and_fail_modes() -> None:
    """RedisRateLimiter Lua çağrısını güvenli yapmalı, fail-open/closed modlarını izlemelidir."""
    recorded_calls: list[tuple[list[str], list[Any]]] = []

    class _FakeScript:
        def __init__(self, should_fail: bool = False) -> None:
            self.should_fail = should_fail

        async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
            recorded_calls.append((keys, args))
            if self.should_fail:
                raise RedisConnectionError("redis down")
            return [1, 4, 58500] if len(recorded_calls) == 1 else [0, 0, 42100]

    class _FakeRedis:
        def __init__(self, should_fail: bool = False) -> None:
            self.should_fail = should_fail
            self.closed = False

        def register_script(self, script: str) -> _FakeScript:
            assert "ZREMRANGEBYSCORE" in script
            return _FakeScript(should_fail=self.should_fail)

        async def aclose(self) -> None:
            self.closed = True

    subject = build_rate_limit_subject(auth_digest="digest123", client_ip="10.0.0.5")
    assert "digest123" not in subject

    limiter = RedisRateLimiter(
        client=_FakeRedis(),
        fail_open=True,
        clock=lambda: 1700000000.0,
    )
    ok_decision = await limiter.check_and_consume(
        scope="upload", subject=subject, limit=5, window_seconds=60
    )
    blocked_decision = await limiter.check_and_consume(
        scope="upload", subject=subject, limit=5, window_seconds=60
    )
    assert ok_decision.allowed is True
    assert ok_decision.remaining == 4
    assert ok_decision.reset_after_seconds == 59
    assert blocked_decision.allowed is False
    assert blocked_decision.retry_after_seconds == 43
    assert recorded_calls[0][0] == [f"corerag:rl:upload:{subject}"]

    fail_open_limiter = RedisRateLimiter(client=_FakeRedis(should_fail=True), fail_open=True)
    fallback = await fail_open_limiter.check_and_consume(
        scope="query", subject="subj", limit=20, window_seconds=60
    )
    assert fallback.allowed is True
    assert fallback.remaining == 20

    fail_closed_limiter = RedisRateLimiter(client=_FakeRedis(should_fail=True), fail_open=False)
    with pytest.raises(ServiceUnavailableError):
        await fail_closed_limiter.check_and_consume(
            scope="query", subject="subj", limit=20, window_seconds=60
        )


async def test_rate_limit_enforced_across_upload_delete_query_and_sse_stream(
    app: FastAPI,
    client: httpx.AsyncClient,
    unauthenticated_client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Upload/Delete/Query/SSE uçları 429 ve X-RateLimit-* dönmeli; 401 kota harcamamalıdır."""
    rate_settings = test_settings.model_copy(
        update={
            "RATE_LIMIT_ENABLED": True,
            "RATE_LIMIT_WINDOW_SECONDS": 60,
            "RATE_LIMIT_UPLOAD_PER_WINDOW": 2,
            "RATE_LIMIT_DELETE_PER_WINDOW": 1,
            "RATE_LIMIT_QUERY_PER_WINDOW": 2,
            "RATE_LIMIT_READ_PER_WINDOW": 2,
        }
    )
    limiter = InMemoryRateLimiter()
    app.dependency_overrides[get_settings] = lambda: rate_settings
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_vector_db] = lambda: _StubVectorDB()
    app.dependency_overrides[get_llm_client] = lambda: _StubLLMClient()

    # 1. Yetkisiz çağrılar 401 dönmeli ve authenticated kotadan yememelidir
    for _ in range(5):
        unauth_resp = await unauthenticated_client.get("/api/v1/documents/tenants")
        assert unauth_resp.status_code == 401

    read_1 = await client.get("/api/v1/documents/tenants")
    assert read_1.status_code == 200
    assert read_1.headers["x-ratelimit-limit"] == "2"
    assert read_1.headers["x-ratelimit-remaining"] == "1"

    # 2. Upload limiti (2 / 60s): 3. istek 429 ve diske dosya yazmaz
    for idx in range(2):
        up_ok = await client.post(
            "/api/v1/documents/upload",
            data={"tenant_id": "acme_corp"},
            files={"file": (f"ok_{idx}.txt", b"hello world", "text/plain")},
        )
        assert up_ok.status_code == 202
        assert up_ok.headers["x-ratelimit-remaining"] == str(1 - idx)

    files_before_blocked_upload = len(list(test_settings.UPLOAD_DIR.iterdir()))
    up_blocked = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("blocked.txt", b"should not persist", "text/plain")},
    )
    assert up_blocked.status_code == 429
    assert up_blocked.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(up_blocked.headers["retry-after"]) >= 1
    assert up_blocked.headers["x-ratelimit-remaining"] == "0"
    assert up_blocked.headers["x-ratelimit-limit"] == "2"
    assert len(list(test_settings.UPLOAD_DIR.iterdir())) == files_before_blocked_upload

    # 3. Delete limiti (1 / 60s)
    del_ok = await client.delete("/api/v1/documents/doc-1", params={"tenant_id": "acme_corp"})
    assert del_ok.status_code == 200
    del_blocked = await client.delete("/api/v1/documents/doc-2", params={"tenant_id": "acme_corp"})
    assert del_blocked.status_code == 429
    assert del_blocked.json()["error"]["code"] == "rate_limit_exceeded"

    # 4. Query + SSE Stream limiti (2 / 60s) ve StreamingResponse header aktarımı
    q_json = await client.post(
        "/api/v1/query",
        json={"query": "Politika nedir?", "tenant_id": "acme_corp", "stream": False},
    )
    assert q_json.status_code == 200
    assert q_json.headers["x-ratelimit-limit"] == "2"
    assert q_json.headers["x-ratelimit-remaining"] == "1"

    q_stream = await client.post(
        "/api/v1/query/stream",
        json={"query": "Politika nedir?", "tenant_id": "acme_corp", "stream": True},
    )
    assert q_stream.status_code == 200
    assert q_stream.headers["x-ratelimit-limit"] == "2"
    assert q_stream.headers["x-ratelimit-remaining"] == "0"
    assert "x-ratelimit-reset" in q_stream.headers

    q_blocked = await client.post(
        "/api/v1/query/stream",
        json={"query": "Tekrar sor", "tenant_id": "acme_corp", "stream": True},
    )
    assert q_blocked.status_code == 429
    assert q_blocked.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(q_blocked.headers["retry-after"]) >= 1
