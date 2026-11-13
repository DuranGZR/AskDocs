import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.core.config import get_settings
from src.core.logging import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Uygulama açılış ve kapanışında altyapı kaynaklarını güvenli şekilde yönetir."""
    settings = get_settings()
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Starting %s (env=%s, upload_dir=%s)",
        settings.APP_NAME,
        settings.APP_ENV,
        settings.UPLOAD_DIR,
    )

    try:
        yield
    finally:
        logger.info("Shutting down %s gracefully", settings.APP_NAME)


def create_app() -> FastAPI:
    """FastAPI uygulama örneğini yapılandırıp döndüren factory fonksiyonu."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)

    is_prod = settings.APP_ENV == "production"
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        debug=settings.DEBUG,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.state.settings = settings
    return app


app = create_app()