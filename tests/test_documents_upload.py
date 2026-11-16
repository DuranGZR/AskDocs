from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import httpx
import pytest
from fastapi import FastAPI, Request

from src.api.dependencies import get_storage_service, get_task_dispatcher
from src.core.config import Settings
from src.core.exceptions import register_exception_handlers
from src.core.middleware import MaxBodySizeMiddleware, RequestIdMiddleware
from src.services.base import TaskStatusSnapshot
from src.services.storage_service import LocalStorageService


class _RecordedDispatcher(Protocol):
    calls: list[dict[str, object]]
    snapshots: dict[str, TaskStatusSnapshot]


async def test_upload_valid_txt_writes_uuid_file_without_path_traversal(
    client: httpx.AsyncClient,
    test_settings: Settings,
    task_dispatcher: _RecordedDispatcher,
) -> None:
    """Geçerli yükleme UUID dosya adıyla yazılmalı, kuyruğa atılmalı ve kaçışı önlemelidir."""
    payload = b"CoreRAG-Engine integration stream test"
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("../../secrets/notes.txt", payload, "text/plain")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] == f"test-task-{body['document_id']}"
    assert body["tenant_id"] == "acme_corp"
    assert body["filename"] == "notes.txt"
    assert body["file_type"] == "txt"
    assert body["size_bytes"] == len(payload)
    assert body["status"] == "queued"

    saved_files = list(test_settings.UPLOAD_DIR.iterdir())
    assert len(saved_files) == 1
    saved_file = saved_files[0]
    assert saved_file.name == f"{body['document_id']}.txt"
    assert saved_file.read_bytes() == payload

    assert len(task_dispatcher.calls) == 1
    assert task_dispatcher.calls[0]["document_id"] == body["document_id"]
    assert task_dispatcher.calls[0]["tenant_id"] == "acme_corp"
    assert task_dispatcher.calls[0]["file_type"] == "txt"
    assert task_dispatcher.calls[0]["filename"] == "notes.txt"
    assert task_dispatcher.calls[0]["request_id"] == response.headers["x-request-id"]


async def test_upload_valid_pdf_passes_magic_bytes_and_propagates_request_id(
    client: httpx.AsyncClient,
    test_settings: Settings,
    task_dispatcher: _RecordedDispatcher,
) -> None:
    """Geçerli PDF %PDF- imzasını geçmeli, kaydedilmeli ve X-Request-ID taşımalıdır."""
    payload = b"%PDF-1.7\nhello"
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        headers={"X-Request-ID": "upload-pdf-01"},
        files={"file": ("report.pdf", payload, "application/pdf")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["file_type"] == "pdf"
    assert body["task_id"] == f"test-task-{body['document_id']}"
    assert len(list(test_settings.UPLOAD_DIR.iterdir())) == 1
    assert len(task_dispatcher.calls) == 1
    assert task_dispatcher.calls[0]["filename"] == "report.pdf"
    assert task_dispatcher.calls[0]["request_id"] == "upload-pdf-01"


async def test_upload_rejects_spoofed_pdf_without_magic_bytes(
    client: httpx.AsyncClient,
    test_settings: Settings,
    task_dispatcher: _RecordedDispatcher,
) -> None:
    """Uzantısı .pdf olup %PDF- imzası taşımayan dosya 415 ile reddedilmelidir."""
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("fake.pdf", b"MZ\x90\x00not-a-pdf", "application/pdf")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert list(test_settings.UPLOAD_DIR.iterdir()) == []
    assert task_dispatcher.calls == []


async def test_upload_rejects_binary_file_disguised_as_txt(
    client: httpx.AsyncClient,
    test_settings: Settings,
    task_dispatcher: _RecordedDispatcher,
) -> None:
    """Null bayt içeren sahte .txt binary dosyası 415 ile reddedilmelidir."""
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("notes.txt", b"text\x00binary", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert list(test_settings.UPLOAD_DIR.iterdir()) == []
    assert task_dispatcher.calls == []


async def test_upload_cleans_up_stored_file_and_returns_503_when_queue_fails(
    app: FastAPI,
    client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Kuyruğa atma başarısız olursa kaydedilen dosya silinmeli ve 503 dönmelidir."""

    class _FailingDispatcher:
        async def enqueue_document(
            self,
            *,
            document_id: str,
            file_path: Path,
            tenant_id: str,
            file_type: str,
            request_id: str,
            filename: str = "",
        ) -> str:
            raise RuntimeError("broker unreachable")

        async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
            return TaskStatusSnapshot(task_id=task_id, status="PENDING")

    app.dependency_overrides[get_task_dispatcher] = lambda: _FailingDispatcher()

    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": "acme_corp"},
        files={"file": ("valid.pdf", b"%PDF-1.4 content", "application/pdf")},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert list(test_settings.UPLOAD_DIR.iterdir()) == []


async def test_get_document_task_status_returns_lifecycle_states(
    client: httpx.AsyncClient,
    task_dispatcher: _RecordedDispatcher,
) -> None:
    """Görev durumu PENDING, STARTED, SUCCESS ve temizlenmiş FAILURE durumlarını dönmelidir."""
    pending_resp = await client.get("/api/v1/documents/status/task-pending-1")
    assert pending_resp.status_code == 200
    assert pending_resp.json() == {
        "task_id": "task-pending-1",
        "status": "PENDING",
        "result": None,
        "error": None,
    }

    task_dispatcher.snapshots["task-started-1"] = TaskStatusSnapshot(
        task_id="task-started-1",
        status="STARTED",
    )
    started_resp = await client.get("/api/v1/documents/status/task-started-1")
    assert started_resp.status_code == 200
    assert started_resp.json()["status"] == "STARTED"

    task_dispatcher.snapshots["task-ok-1"] = TaskStatusSnapshot(
        task_id="task-ok-1",
        status="SUCCESS",
        result={"document_id": "doc-1", "status": "processed_stub"},
    )
    ok_resp = await client.get("/api/v1/documents/status/task-ok-1")
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {
        "task_id": "task-ok-1",
        "status": "SUCCESS",
        "result": {"document_id": "doc-1", "status": "processed_stub"},
        "error": None,
    }

    task_dispatcher.snapshots["task-fail-1"] = TaskStatusSnapshot(
        task_id="task-fail-1",
        status="FAILURE",
        result=None,
        error="Document processing failed",
    )
    fail_resp = await client.get("/api/v1/documents/status/task-fail-1")
    assert fail_resp.status_code == 200
    assert fail_resp.json() == {
        "task_id": "task-fail-1",
        "status": "FAILURE",
        "result": None,
        "error": "Document processing failed",
    }


async def test_get_document_task_status_rejects_invalid_task_id(
    client: httpx.AsyncClient,
) -> None:
    """Geçersiz karakter içeren task_id yolu 422 validation_error döndürmelidir."""
    response = await client.get("/api/v1/documents/status/invalid!id")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_get_document_task_status_returns_503_when_backend_fails(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """Sonuç arka ucu erişilemezse 503 service_unavailable döndürmelidir."""

    class _UnavailableBackendDispatcher:
        async def enqueue_document(
            self,
            *,
            document_id: str,
            file_path: Path,
            tenant_id: str,
            file_type: str,
            request_id: str,
            filename: str = "",
        ) -> str:
            return f"task-{document_id}"

        async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
            raise RuntimeError("redis result backend timeout")

    app.dependency_overrides[get_task_dispatcher] = lambda: _UnavailableBackendDispatcher()

    response = await client.get("/api/v1/documents/status/task-123")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


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
