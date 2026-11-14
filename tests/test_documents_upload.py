from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

from src.api.dependencies import get_storage_service
from src.core.config import Settings
from src.core.exceptions import register_exception_handlers
from src.core.middleware import MaxBodySizeMiddleware, RequestIdMiddleware
from src.services.storage_service import LocalStorageService


async def test_upload_valid_txt_writes_uuid_file_without_path_traversal(
    client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Geçerli yükleme UUID dosya adıyla yazılmalı ve dizin kaçışını engellemelidir."""
    payload = b"CoreRAG-Engine integration stream test"
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("../../secrets/notes.txt", payload, "text/plain")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] is None
    assert body["tenant_id"] == "acme_corp"
    assert body["filename"] == "notes.txt"
    assert body["file_type"] == "txt"
    assert body["size_bytes"] == len(payload)
    assert body["status"] == "stored"

    saved_files = list(test_settings.UPLOAD_DIR.iterdir())
    assert len(saved_files) == 1
    saved_file = saved_files[0]
    assert saved_file.name == f"{body['document_id']}.txt"
    assert saved_file.read_bytes() == payload


@pytest.mark.parametrize("filename", ["malware.exe", "   .pdf", "archive.zip"])
async def test_upload_rejects_unsupported_extension_without_leftover_files(
    client: httpx.AsyncClient,
    test_settings: Settings,
    filename: str,
) -> None:
    """Desteklenmeyen uzantılar 415 ile reddedilmeli ve diskte dosya kalmamalıdır."""
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": (filename, b"MZ", "application/octet-stream")},
    )

    assert response.status_code == 415
    body = response.json()
    assert body["error"]["code"] == "unsupported_media_type"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert list(test_settings.UPLOAD_DIR.iterdir()) == []


async def test_upload_rejects_empty_file_and_cleans_disk(
    client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Boş dosya 400 ile reddedilmeli ve diskte 0 baytlık dosya bırakılmamalıdır."""
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("empty.txt", b"", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert response.json()["error"]["message"] == "Uploaded file is empty"
    assert list(test_settings.UPLOAD_DIR.iterdir()) == []


async def test_upload_enforces_size_limit_and_cleans_partial_file(
    app: FastAPI,
    client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Boyut sınırı aşıldığında 413 dönmeli ve yarım dosya diskten silinmelidir."""
    app.dependency_overrides[get_storage_service] = lambda: LocalStorageService(
        base_dir=test_settings.UPLOAD_DIR,
        max_size_bytes=10,
        chunk_size=4,
    )

    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("large.txt", b"0123456789ABCDEF", "text/plain")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert list(test_settings.UPLOAD_DIR.iterdir()) == []


async def test_max_body_size_middleware_guards_headers_and_chunked_stream() -> None:
    """MaxBodySizeMiddleware büyük başlığı, chunked taşmayı ve bozuk Content-Length'i keser."""
    mini_app = FastAPI()
    register_exception_handlers(mini_app)

    @mini_app.post("/echo")
    async def echo(request: Request) -> dict[str, bool]:
        await request.body()
        return {"ok": True}

    wrapped_app = RequestIdMiddleware(MaxBodySizeMiddleware(mini_app, max_body_bytes=10))

    async def chunked_stream() -> AsyncIterator[bytes]:
        yield b"123456"
        yield b"789012"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped_app),
        base_url="http://test",
    ) as mini_client:
        header_resp = await mini_client.post("/echo", headers={"Content-Length": "999999"})
        assert header_resp.status_code == 413
        assert header_resp.json()["error"]["code"] == "payload_too_large"

        chunked_resp = await mini_client.post("/echo", content=chunked_stream())
        assert chunked_resp.status_code == 413
        assert chunked_resp.json()["error"]["code"] == "payload_too_large"
        assert "x-request-id" in chunked_resp.headers

        bad_len_resp = await mini_client.post(
            "/echo",
            content=b"hi",
            headers={"Content-Length": "invalid-int"},
        )
        assert bad_len_resp.status_code == 400
        assert bad_len_resp.json()["error"]["code"] == "bad_request"
