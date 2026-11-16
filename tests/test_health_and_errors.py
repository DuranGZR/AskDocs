from pathlib import Path

import httpx
from fastapi import FastAPI

from src.core.config import Settings, get_settings
from src.core.exceptions import AppError


async def test_health_check_ok(client: httpx.AsyncClient) -> None:
    """Tüm bağımlılıklar hazırken 200 OK, checks/circuits özeti ve Request-ID dönmelidir."""
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app_name": "CoreRAG-Engine",
        "version": "0.1.0",
        "environment": "development",
        "checks": {"storage": "ok", "redis": "ok", "qdrant": "ok"},
        "circuits": {"qdrant": "closed", "llm": "closed"},
    }
    request_id = response.headers.get("x-request-id")
    assert request_id is not None
    assert len(request_id) == 32


async def test_health_check_degraded_returns_503(
    app: FastAPI,
    client: httpx.AsyncClient,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Depolama dizini yoksa health ucu degraded durumu ve 503 dönmelidir."""
    degraded_settings = test_settings.model_copy(
        update={"UPLOAD_DIR": tmp_path / "non_existent_dir"}
    )
    app.dependency_overrides[get_settings] = lambda: degraded_settings

    response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "app_name": "CoreRAG-Engine",
        "version": "0.1.0",
        "environment": "development",
        "checks": {"storage": "unavailable", "redis": "ok", "qdrant": "ok"},
        "circuits": {"qdrant": "closed", "llm": "closed"},
    }


async def test_unknown_route_returns_standard_error_envelope(
    client: httpx.AsyncClient,
) -> None:
    """Bulunamayan endpoint standart ErrorResponse formatı ve eşleşen request_id dönmelidir."""
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body == {
        "error": {
            "code": "http_404",
            "message": "Not Found",
            "request_id": response.headers["x-request-id"],
        }
    }


async def test_request_id_passthrough_and_unsafe_header_rejection(
    client: httpx.AsyncClient,
) -> None:
    """Geçerli X-Request-ID korunmalı, güvensiz başlık yeni UUID ile değiştirilmelidir."""
    valid_response = await client.get(
        "/api/v1/does-not-exist",
        headers={"X-Request-ID": "trace-abc_123.01"},
    )
    assert valid_response.headers["x-request-id"] == "trace-abc_123.01"
    assert valid_response.json()["error"]["request_id"] == "trace-abc_123.01"

    unsafe_response = await client.get(
        "/api/v1/does-not-exist",
        headers={"X-Request-ID": "bad header!"},
    )
    generated_id = unsafe_response.headers["x-request-id"]
    assert generated_id != "bad header!"
    assert len(generated_id) == 32
    assert unsafe_response.json()["error"]["request_id"] == generated_id


async def test_validation_error_returns_standard_422_with_details(
    client: httpx.AsyncClient,
) -> None:
    """Geçersiz tenant_id isteği standart 422 validation_error detayı döndürmelidir."""
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "../hack"},
        files={"file": ("doc.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Request validation failed"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert any(detail["field"] == "body.tenant_id" for detail in body["error"]["details"])


async def test_unhandled_exception_returns_safe_500_with_request_id(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """Beklenmeyen 500 hatası iç detayı sızdırmadan güvenli JSON ve X-Request-ID dönmelidir."""

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive internal connection string")

    response = await client.get("/_test/boom", headers={"X-Request-ID": "boom-500"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "boom-500"
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "An unexpected internal error occurred",
            "request_id": "boom-500",
        }
    }


async def test_app_error_preserves_custom_headers(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """AppError üzerindeki özel HTTP başlıkları hata yanıtına eksiksiz taşınmalıdır."""

    @app.get("/_test/auth-error")
    async def auth_error() -> None:
        raise AppError(
            "Authentication required",
            status_code=401,
            code="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    response = await client.get("/_test/auth-error")

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


async def test_security_headers_and_static_ui_cache_control(
    client: httpx.AsyncClient,
) -> None:
    """API ve UI yanıtları güvenlik başlıkları taşımalı, UI varlıkları no-cache dönmelidir."""
    health_resp = await client.get("/api/v1/health")
    assert health_resp.status_code == 200
    assert health_resp.headers.get("x-content-type-options") == "nosniff"
    assert health_resp.headers.get("x-frame-options") == "DENY"
    assert health_resp.headers.get("referrer-policy") == "no-referrer"
    assert "cache-control" not in health_resp.headers

    root_resp = await client.get("/")
    assert root_resp.status_code == 200
    assert root_resp.headers.get("cache-control") == "no-cache"
    assert root_resp.headers.get("x-content-type-options") == "nosniff"
    assert root_resp.headers.get("x-frame-options") == "DENY"
    assert root_resp.headers.get("referrer-policy") == "no-referrer"

    js_resp = await client.get("/static/app.js")
    assert js_resp.status_code == 200
    assert js_resp.headers.get("cache-control") == "no-cache"
    assert js_resp.headers.get("x-content-type-options") == "nosniff"
