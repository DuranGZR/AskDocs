import os
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from src.api.dependencies import get_task_dispatcher
from src.core.config import Settings, get_settings
from src.services.base import TaskStatusSnapshot


class RecordingTaskDispatcher:
    """Testlerde Redis bağlantısı kurmadan kuyruk çağrılarını kaydeder."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.snapshots: dict[str, TaskStatusSnapshot] = {}

    async def enqueue_document(
        self,
        *,
        document_id: str,
        file_path: Path,
        tenant_id: str,
        file_type: str,
        request_id: str,
    ) -> str:
        self.calls.append(
            {
                "document_id": document_id,
                "file_path": file_path,
                "tenant_id": tenant_id,
                "file_type": file_type,
                "request_id": request_id,
            }
        )
        task_id = f"test-task-{document_id}"
        self.snapshots[task_id] = TaskStatusSnapshot(task_id=task_id, status="PENDING")
        return task_id

    async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
        return self.snapshots.get(task_id, TaskStatusSnapshot(task_id=task_id, status="PENDING"))


def pytest_configure(config: pytest.Config) -> None:
    """CI gibi .env dosyası olmayan ortamlarda varsayılan test anahtarı sağlar."""
    os.environ.setdefault("API_KEY", "test-api-key-minimum-8-chars")


@pytest.fixture
def test_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Her test için izole geçici upload diziniyle deterministik ayarlar üretir."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_NAME", "CoreRAG-Engine")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("API_KEY", "test-api-key-minimum-8-chars")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def task_dispatcher() -> RecordingTaskDispatcher:
    """Her test için kuyruğa atılan görev argümanlarını kaydeden sahte dispatcher döner."""
    return RecordingTaskDispatcher()


@pytest.fixture
def app(
    test_settings: Settings,
    task_dispatcher: RecordingTaskDispatcher,
) -> Iterator[FastAPI]:
    """Her test fonksiyonuna bağımsız bir FastAPI uygulama örneği sağlar."""
    from src.main import create_app

    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.dependency_overrides[get_task_dispatcher] = lambda: task_dispatcher
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """ASGITransport ile ağ açmadan doğrudan bellekte asenkron HTTP istemcisi sağlar."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
