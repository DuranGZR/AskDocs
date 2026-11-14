from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config import Settings, get_settings
from src.tasks.celery_app import create_celery_app
from src.tasks.document_tasks import process_document_task


def test_settings_validates_redis_and_celery_urls() -> None:
    """Redis/Celery URL'leri boşluktan arındırılmalı ve geçerli host/port içermelidir."""
    settings = Settings(
        _env_file=None,
        API_KEY="test-api-key-minimum-8-chars",
        REDIS_URL="  redis://redis:6379/0  ",
        CELERY_BROKER_URL="rediss://redis:6380/1",
        CELERY_RESULT_BACKEND="redis://redis:6379/2",
    )

    assert settings.REDIS_URL == "redis://redis:6379/0"
    assert settings.CELERY_BROKER_URL == "rediss://redis:6380/1"
    assert settings.CELERY_RESULT_BACKEND == "redis://redis:6379/2"


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost:6379/1",
        "redis://",
        "redis:///0",
        "redis://localhost:notaport/1",
    ],
)
def test_settings_rejects_invalid_redis_urls(bad_url: str) -> None:
    """Geçersiz şema, eksik host veya hatalı port içeren Redis URL'leri reddedilmelidir."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            API_KEY="test-api-key-minimum-8-chars",
            CELERY_BROKER_URL=bad_url,
        )


def test_celery_app_uses_safe_json_and_reliability_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Celery uygulaması Pickle'ı kapalı, reliable acks açık ve task kayıtlı kurulmalıdır."""
    monkeypatch.setenv("API_KEY", "test-api-key-minimum-8-chars")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://test-broker:6379/4")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://test-backend:6379/5")

    get_settings.cache_clear()
    try:
        app = create_celery_app()
    finally:
        get_settings.cache_clear()

    assert app.conf.broker_url == "redis://test-broker:6379/4"
    assert app.conf.result_backend == "redis://test-backend:6379/5"
    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_track_started is True
    assert app.conf.timezone == "UTC"
    assert app.conf.enable_utc is True
    assert "corerag.process_document" in app.tasks


def test_process_document_task_reports_file_presence_and_size(tmp_path: Path) -> None:
    """Task mevcut dosyada boyutu doğru raporlamalı, eksik dosyada çökmemelidir."""
    sample = tmp_path / "sample.pdf"
    content = b"%PDF-1.4 demo content"
    sample.write_bytes(content)

    existing_result = process_document_task(
        document_id="doc-1",
        file_path=str(sample),
        tenant_id="tenant-a",
        file_type="pdf",
    )
    assert existing_result == {
        "document_id": "doc-1",
        "tenant_id": "tenant-a",
        "file_type": "pdf",
        "file_exists": True,
        "size_bytes": len(content),
        "status": "processed_stub",
    }

    missing_result = process_document_task(
        document_id="doc-2",
        file_path=str(tmp_path / "missing.pdf"),
        tenant_id="tenant-a",
        file_type="pdf",
    )
    assert missing_result["file_exists"] is False
    assert missing_result["size_bytes"] == 0
    assert missing_result["status"] == "processed_stub"
