import logging
import re
import time
import uuid

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.exceptions import (
    AppError,
    BadRequestError,
    PayloadTooLargeError,
    app_error_handler,
    unhandled_exception_handler,
)
from src.core.logging import request_id_ctx

logger = logging.getLogger(__name__)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RequestIdMiddleware:
    """Her HTTP isteğine güvenli bir request_id atar, bağlama yazar ve erişim logu üretir."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._extract_or_generate_request_id(scope)
        token = request_id_ctx.set(request_id)
        start_time = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "%s %s -> %d (%.1fms)",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_code,
                duration_ms,
            )
            request_id_ctx.reset(token)

    @staticmethod
    def _extract_or_generate_request_id(scope: Scope) -> str:
        """Gelen istekten güvenli X-Request-ID başlığını alır veya yeni bir UUID üretir."""
        for name, value in scope.get("headers") or ():
            if bytes(name).lower() == b"x-request-id":
                candidate = bytes(value).decode("latin-1").strip()
                if _REQUEST_ID_PATTERN.fullmatch(candidate):
                    return candidate
                break
        return uuid.uuid4().hex


class MaxBodySizeMiddleware:
    """Aşırı büyük HTTP gövdelerini multipart ayrıştırmadan önce erken reddeder."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length_error = self._validate_content_length(scope)
        if content_length_error is not None:
            request = Request(scope, receive)
            response = await app_error_handler(request, content_length_error)
            await response(scope, receive, send)
            return

        received_bytes = 0
        response_started = False

        async def receive_wrapper() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise PayloadTooLargeError(
                        f"Request body exceeds maximum allowed size of {self.max_body_bytes} bytes"
                    )
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except AppError as exc:
            if response_started:
                raise
            request = Request(scope, receive)
            response = await app_error_handler(request, exc)
            await response(scope, receive, send)

    def _validate_content_length(self, scope: Scope) -> AppError | None:
        """Varsa Content-Length başlığını sınır ve geçerlilik açısından kontrol eder."""
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                raw_value = value.decode("latin-1").strip()
                try:
                    content_length = int(raw_value)
                except ValueError:
                    return BadRequestError("Invalid Content-Length header")
                if content_length < 0:
                    return BadRequestError("Invalid Content-Length header")
                if content_length > self.max_body_bytes:
                    return PayloadTooLargeError(
                        f"Request body exceeds maximum allowed size of {self.max_body_bytes} bytes"
                    )
                break
        return None


class UnhandledExceptionMiddleware:
    """Beklenmeyen 500 hatalarını request_id bağlamı sıfırlanmadan JSON yanıtına çevirir."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                raise
            request = Request(scope, receive)
            response = await unhandled_exception_handler(request, exc)
            await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """Tüm HTTP yanıtlarına temel güvenlik başlıklarını ve UI dosyalarına no-cache ekler."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        is_ui_asset = path == "/" or path.startswith("/static/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if is_ui_asset:
                    headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_wrapper)
