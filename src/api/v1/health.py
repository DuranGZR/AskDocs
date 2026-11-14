import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Request, Response, status

from src.api.dependencies import SettingsDep
from src.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


def _is_writable_dir(path: Path) -> bool:
    """Dizinin mevcut ve yazılabilir olup olmadığını kontrol eder."""
    return path.is_dir() and os.access(path, os.W_OK)


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
    summary="Check service health",
)
async def health_check(
    request: Request,
    response: Response,
    settings: SettingsDep,
) -> HealthResponse:
    """Uygulamanın temel çalışma ve depolama dizini yazılabilirliğini kontrol eder."""
    storage_ok = await asyncio.to_thread(_is_writable_dir, settings.UPLOAD_DIR)
    if not storage_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if storage_ok else "degraded",
        app_name=settings.APP_NAME,
        version=request.app.version,
        environment=settings.APP_ENV,
        checks={"storage": "ok" if storage_ok else "unavailable"},
    )
