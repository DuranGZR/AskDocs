import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.v1.router import api_router
from src.core.config import get_settings
from src.core.exceptions import register_exception_handlers
from src.core.logging import setup_logging
from src.core.middleware import (
    MaxBodySizeMiddleware,
    RequestIdMiddleware,
    UnhandledExceptionMiddleware,
)
from src.core.observability import configure_observability, shutdown_observability
from src.services.llm_service import AsyncOpenAICompatibleLLMClient
from src.services.qdrant_service import AsyncQdrantService
from src.services.rate_limiter import RedisRateLimiter

logger = logging.getLogger(__name__)
_UI_DIR = Path(__file__).resolve().parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Uygulama açılış ve kapanışında altyapı kaynaklarını güvenli şekilde yönetir."""
    settings = get_settings()
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    vector_db = AsyncQdrantService(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
        vector_size=settings.QDRANT_VECTOR_SIZE,
    )
    await vector_db.ensure_collection()
    app.state.vector_db = vector_db

    llm_client = AsyncOpenAICompatibleLLMClient(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        llm_model=settings.LLM_MODEL,
        embedding_model=settings.EMBEDDING_MODEL,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
    )
    app.state.llm_client = llm_client
    rate_limiter = RedisRateLimiter(
        redis_url=settings.REDIS_URL,
        timeout_seconds=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        fail_open=settings.RATE_LIMIT_FAIL_OPEN,
    )
    app.state.rate_limiter = rate_limiter
    configure_observability(settings)
    logger.info(
        "Starting %s (env=%s, upload_dir=%s, qdrant_collection=%s, llm_model=%s)",
        settings.APP_NAME,
        settings.APP_ENV,
        settings.UPLOAD_DIR,
        settings.QDRANT_COLLECTION_NAME,
        settings.LLM_MODEL,
    )

    try:
        yield
    finally:
        shutdown_observability()
        await rate_limiter.close()
        await llm_client.close()
        await vector_db.close()
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
    register_exception_handlers(app)
    app.include_router(api_router)

    if _UI_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_UI_DIR), name="ui-static")

    @app.get("/", include_in_schema=False)
    async def serve_ui() -> FileResponse:
        """Cupertino Light tek sayfa Web arayüzünü sunar."""
        return FileResponse(_UI_DIR / "index.html")

    # Starlette middleware'leri ters sırada sarar (en son eklenen en dışta çalışır).
    # Multipart başlık/sınır payı için upload limitine 1 MiB ek tampon eklenir.
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_body_bytes=settings.max_upload_size_bytes + 1024 * 1024,
    )
    app.add_middleware(UnhandledExceptionMiddleware)
    app.add_middleware(RequestIdMiddleware)

    return app


app = create_app()
