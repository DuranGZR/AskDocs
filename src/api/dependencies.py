import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, Request, Response, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from src.core.circuit_breaker import CircuitBreaker
from src.core.config import Settings, get_settings
from src.core.exceptions import RateLimitExceededError, UnauthorizedError
from src.services.base import (
    BaseHealthChecker,
    BaseLLMClient,
    BaseRateLimiter,
    BaseStorage,
    BaseVectorDB,
    DocumentTaskDispatcher,
)
from src.services.health_service import DependencyHealthChecker
from src.services.llm_service import AsyncOpenAICompatibleLLMClient
from src.services.qdrant_service import AsyncQdrantService
from src.services.rate_limiter import RedisRateLimiter, build_rate_limit_subject
from src.services.storage_service import LocalStorageService
from src.tasks.document_tasks import CeleryDocumentTaskDispatcher

logger = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]

_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="API key supplied via X-API-Key request header",
)
_bearer_auth = HTTPBearer(
    auto_error=False,
    description="API key supplied as Authorization Bearer token",
)

RateLimitScope = Literal["upload", "delete", "query", "read"]

_RATE_LIMIT_ATTR_BY_SCOPE: dict[RateLimitScope, str] = {
    "upload": "RATE_LIMIT_UPLOAD_PER_WINDOW",
    "delete": "RATE_LIMIT_DELETE_PER_WINDOW",
    "query": "RATE_LIMIT_QUERY_PER_WINDOW",
    "read": "RATE_LIMIT_READ_PER_WINDOW",
}


def _constant_time_api_key_match(*, provided: str, expected: str) -> bool:
    """İki API anahtarını sabit zamanlı SHA-256 özet karşılaştırmasıyla doğrular."""
    if not provided or not expected:
        return False
    provided_digest = hashlib.sha256(provided.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(provided_digest, expected_digest)


async def verify_api_key(
    request: Request,
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Security(_api_key_header)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_auth)] = None,
) -> str:
    """İstekteki X-API-Key veya Bearer belirtecini doğrular; ham anahtar yerine özet döner."""
    raw_candidate = (
        x_api_key if x_api_key is not None else (bearer.credentials if bearer is not None else "")
    )
    candidate = raw_candidate.strip()
    if not candidate:
        logger.warning(
            "Authentication rejected on %s %s (reason=missing_api_key)",
            request.method,
            request.url.path,
        )
        raise UnauthorizedError("Missing API key")

    expected = settings.API_KEY.get_secret_value()
    if not _constant_time_api_key_match(provided=candidate, expected=expected):
        logger.warning(
            "Authentication rejected on %s %s (reason=invalid_api_key)",
            request.method,
            request.url.path,
        )
        raise UnauthorizedError("Invalid API key")

    return hashlib.sha256(expected.encode("utf-8")).hexdigest()[:24]


def get_storage_service(settings: SettingsDep) -> BaseStorage:
    """Ayarlardaki dizin ve boyut limitiyle dosya depolama servisini oluşturur."""
    return LocalStorageService(
        base_dir=settings.UPLOAD_DIR,
        max_size_bytes=settings.max_upload_size_bytes,
    )


StorageDep = Annotated[BaseStorage, Depends(get_storage_service)]


def get_task_dispatcher() -> DocumentTaskDispatcher:
    """Doküman işleme görevlerini Celery kuyruğuna ileten servisi oluşturur."""
    return CeleryDocumentTaskDispatcher()


TaskDispatcherDep = Annotated[DocumentTaskDispatcher, Depends(get_task_dispatcher)]


def build_circuit_breaker(*, name: str, settings: Settings) -> CircuitBreaker:
    """Ayarlarla yapılandırılmış asenkron 3 durumlu devre kesici örneği oluşturur."""
    return CircuitBreaker(
        name=name,
        enabled=settings.CIRCUIT_BREAKER_ENABLED,
        failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout_seconds=settings.CIRCUIT_BREAKER_RECOVERY_SECONDS,
        half_open_max_calls=settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS,
        half_open_success_threshold=settings.CIRCUIT_BREAKER_HALF_OPEN_SUCCESS_THRESHOLD,
    )


def _get_or_create_circuit_breaker(
    request: Request,
    *,
    state_attr: str,
    circuit_name: str,
    settings: Settings,
) -> CircuitBreaker:
    """İstek uygulamasının state alanında devre kesiciyi tekilleştirip döndürür."""
    breaker: CircuitBreaker | None = getattr(request.app.state, state_attr, None)
    if breaker is None:
        breaker = build_circuit_breaker(name=circuit_name, settings=settings)
        setattr(request.app.state, state_attr, breaker)
    return breaker


def get_qdrant_circuit_breaker(request: Request, settings: SettingsDep) -> CircuitBreaker:
    """Uygulama kapsamındaki Qdrant devre kesici örneğini döndürür."""
    return _get_or_create_circuit_breaker(
        request,
        state_attr="qdrant_circuit_breaker",
        circuit_name="qdrant",
        settings=settings,
    )


QdrantCircuitBreakerDep = Annotated[CircuitBreaker, Depends(get_qdrant_circuit_breaker)]


def get_llm_circuit_breaker(request: Request, settings: SettingsDep) -> CircuitBreaker:
    """Uygulama kapsamındaki LLM/Embedding devre kesici örneğini döndürür."""
    return _get_or_create_circuit_breaker(
        request,
        state_attr="llm_circuit_breaker",
        circuit_name="llm",
        settings=settings,
    )


LLMCircuitBreakerDep = Annotated[CircuitBreaker, Depends(get_llm_circuit_breaker)]


def get_vector_db(request: Request, settings: SettingsDep) -> BaseVectorDB:
    """Lifespan'de hazırlanan paylaşımlı asenkron Qdrant istemcisini döndürür."""
    vector_db: BaseVectorDB | None = getattr(request.app.state, "vector_db", None)
    if vector_db is not None:
        return vector_db
    return AsyncQdrantService(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
        vector_size=settings.QDRANT_VECTOR_SIZE,
        circuit_breaker=get_qdrant_circuit_breaker(request, settings),
    )


VectorDBDep = Annotated[BaseVectorDB, Depends(get_vector_db)]


def get_llm_client(request: Request, settings: SettingsDep) -> BaseLLMClient:
    """Lifespan'de hazırlanan paylaşımlı asenkron LLM/Embedding istemcisini döndürür."""
    llm_client: BaseLLMClient | None = getattr(request.app.state, "llm_client", None)
    if llm_client is not None:
        return llm_client
    return AsyncOpenAICompatibleLLMClient(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        llm_model=settings.LLM_MODEL,
        embedding_model=settings.EMBEDDING_MODEL,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
        circuit_breaker=get_llm_circuit_breaker(request, settings),
    )


LLMClientDep = Annotated[BaseLLMClient, Depends(get_llm_client)]


def get_rate_limiter(request: Request, settings: SettingsDep) -> BaseRateLimiter:
    """Lifespan'de hazırlanan paylaşımlı hız sınırlayıcıyı döndürür."""
    limiter: BaseRateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        return limiter
    return RedisRateLimiter(
        redis_url=settings.REDIS_URL,
        timeout_seconds=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        fail_open=settings.RATE_LIMIT_FAIL_OPEN,
    )


RateLimiterDep = Annotated[BaseRateLimiter, Depends(get_rate_limiter)]


def get_health_checker(
    settings: SettingsDep,
    vector_db: VectorDBDep,
    rate_limiter: RateLimiterDep,
) -> BaseHealthChecker:
    """Depolama, Redis ve Qdrant bağımlılıklarını kontrol eden sağlık denetleyicisini döndürür."""
    return DependencyHealthChecker(
        upload_dir=settings.UPLOAD_DIR,
        redis_url=settings.REDIS_URL,
        vector_db=vector_db,
        rate_limiter=rate_limiter,
        timeout_seconds=settings.HEALTH_CHECK_TIMEOUT_SECONDS,
    )


HealthCheckerDep = Annotated[BaseHealthChecker, Depends(get_health_checker)]


def require_rate_limit(scope: RateLimitScope) -> Callable[..., Awaitable[None]]:
    """Kapsam bazlı API hız sınırı bağımlılığı üretir; önce API-Key doğrulanır."""
    limit_attr = _RATE_LIMIT_ATTR_BY_SCOPE[scope]

    async def _enforce_rate_limit(
        request: Request,
        response: Response,
        settings: SettingsDep,
        rate_limiter: RateLimiterDep,
        auth_digest: Annotated[str, Depends(verify_api_key)],
    ) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return

        limit = int(getattr(settings, limit_attr))
        window_seconds = settings.RATE_LIMIT_WINDOW_SECONDS
        client_ip = request.client.host if request.client else "unknown"
        subject = build_rate_limit_subject(auth_digest=auth_digest, client_ip=client_ip)

        decision = await rate_limiter.check_and_consume(
            scope=scope,
            subject=subject,
            limit=limit,
            window_seconds=window_seconds,
        )

        if not decision.allowed:
            logger.warning(
                "Rate limit exceeded on %s %s (scope=%s, limit=%d, retry_after=%ds)",
                request.method,
                request.url.path,
                scope,
                decision.limit,
                decision.retry_after_seconds,
            )
            raise RateLimitExceededError(
                f"Rate limit exceeded for '{scope}'. "
                f"Retry in {decision.retry_after_seconds} seconds.",
                retry_after_seconds=decision.retry_after_seconds,
                limit=decision.limit,
                reset_after_seconds=decision.reset_after_seconds,
            )

        rate_headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(decision.reset_after_seconds),
        }
        response.headers.update(rate_headers)
        request.state.rate_limit_headers = rate_headers

    return _enforce_rate_limit
