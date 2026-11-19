from fastapi import APIRouter, Request, Response, status

from src.api.dependencies import (
    HealthCheckerDep,
    LLMCircuitBreakerDep,
    QdrantCircuitBreakerDep,
    SettingsDep,
)
from src.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthResponse,
            "description": "One or more dependencies are degraded",
        }
    },
    summary="Check service and dependency health",
)
async def health_check(
    request: Request,
    response: Response,
    settings: SettingsDep,
    health_checker: HealthCheckerDep,
    qdrant_breaker: QdrantCircuitBreakerDep,
    llm_breaker: LLMCircuitBreakerDep,
) -> HealthResponse:
    """Depolama, Redis ve Qdrant bağımlılıkları ile devre kesici durumlarını kontrol eder."""
    checks = await health_checker.check_dependencies()
    all_ok = all(value == "ok" for value in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    response.headers["X-RateLimit-Upload-Limit"] = (
        str(settings.RATE_LIMIT_UPLOAD_PER_WINDOW) if settings.RATE_LIMIT_ENABLED else "0"
    )
    response.headers["X-RateLimit-Window-Seconds"] = str(settings.RATE_LIMIT_WINDOW_SECONDS)

    return HealthResponse(
        status="ok" if all_ok else "degraded",
        app_name=settings.APP_NAME,
        version=request.app.version,
        environment=settings.APP_ENV,
        checks=checks,
        circuits={
            "qdrant": qdrant_breaker.state,
            "llm": llm_breaker.state,
        },
    )
