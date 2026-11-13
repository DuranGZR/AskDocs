import re
import uuid

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.exceptions import unhandled_exception_handler
from src.core.logging import request_id_ctx

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RequestIdMiddleware:
    """Her HTTP isteğine güvenli bir request_id atar, bağlama yazar ve yanıta ekler."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._extract_or_generate_request_id(scope)
        token = request_id_ctx.set(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_ctx.reset(token)

    @staticmethod
    def _extract_or_generate_request_id(scope: Scope) -> str:
        """Gelen istekten güvenli X-Request-ID başlığını alır veya yeni bir UUID üretir."""
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                candidate = value.decode("latin-1").strip()
                if _REQUEST_ID_PATTERN.match(candidate):
                    return candidate
                break
        return uuid.uuid4().hex


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