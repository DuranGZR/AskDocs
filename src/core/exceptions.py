import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.core.logging import request_id_ctx
from src.schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

class AppError(Exception):
    """HTTP katmanından bağımsız tüm uygulama ve servis hatalarının temel sınıfı."""
    
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.details = details
        
class BadRequestError(AppError):
    """İstemcinin geçersiz veya hatalı iş kuralı talebi gönderdiğini belirtir."""
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    
class NotFoundError(AppError):
    """İstenen kaynağın (doküman, görev vb.) bulunamadığını belirtir."""
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    
class PayloadTooLargeError(AppError):
    """Yüklenen dosyanın izin verilen maksimum boyutu aştığını belirtir."""
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"
    
class UnsupportedMediaTypeError(AppError):
    """Desteklenmeyen dosya türü veya geçersiz içerik imzası (Magic Bytes) hatası."""
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"
    
class ServiceUnavailableError(AppError):
    """Dış servis (Qdrant, Redis, Ollama/vLLM) veya Circuit Breaker erişilemez hatası."""
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    

def _build_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """Tüm hata yakalayıcılar için tek ve standart JSON hata yanıtı üretir."""
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=request_id_ctx.get(),
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(exclude_none=True),
    )
    
async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Servis katmanından gelen AppError hatalarını standart HTTP JSON yanıtına çevirir."""
    if not isinstance(exc, AppError):
        raise exc
    logger.warning(
        "Application error on %s %s: [%s] %s",
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    return _build_error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )
    
async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI ve Starlette kaynaklı HTTP hatalarını (404, 405 vb.) yakalar."""
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    logger.warning(
        "HTTP exception on %s %s: status=%s detail=%s",
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
    )
    return _build_error_response(
        status_code=exc.status_code,
        code=f"http_{exc.status_code}",
        message=str(exc.detail),
    )
    
async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Pydantic girdi doğrulama hatalarını (422) temiz alan listesiyle döndürür."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    details = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": err.get("msg", "Invalid value"),
            "type": err.get("type", "value_error"),
        }
        for err in exc.errors()
    ]
    logger.warning(
        "Validation failed on %s %s: %d error(s)",
        request.method,
        request.url.path,
        len(details),
    )
    return _build_error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="Request validation failed",
        details=details,
    )
    
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Beklenmeyen tüm Python hatalarını loglar ve istemciye güvenli 500 yanıtı döner."""
    logger.exception(
        "Unhandled server error on %s %s (%s)",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return _build_error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_server_error",
        message="An unexpected internal error occurred",
    )
    
def register_exception_handlers(app: FastAPI) -> None:
    """Tüm global hata yakalayıcıları FastAPI uygulamasına kaydeder."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    
    