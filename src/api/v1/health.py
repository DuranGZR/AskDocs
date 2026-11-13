from fastapi import APIRouter, Request, status

from src.api.dependencies import SettingsDep
from src.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Check service health",
)
async def health_check(request: Request, settings: SettingsDep) -> HealthResponse:
    """Uygulamanın temel çalışma ve depolama dizini sağlığını kontrol eder."""
    storage_ok = settings.UPLOAD_DIR.is_dir()
    return HealthResponse(
        status="ok" if storage_ok else "degraded",
        app_name=settings.APP_NAME,
        version=request.app.version,
        environment=settings.APP_ENV,
        checks={"storage": "ok" if storage_ok else "unavailable"},
    )