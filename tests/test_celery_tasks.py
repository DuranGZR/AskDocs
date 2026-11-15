import logging
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from celery.exceptions import Retry
from pydantic import ValidationError

from src.core.config import Settings, get_settings
from src.core.logging import request_id_ctx
from src.services.pdf_parser import TransientProcessingError
from src.tasks import document_tasks
from src.tasks.celery_app import celery_app, create_celery_app
from src.tasks.document_tasks import CeleryDocumentTaskDispatcher, process_document_task


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
    assert process_document_task.max_retries == 3
    assert process_document_task.retry_backoff is True


def test_process_document_task_reports_file_presence_and_size(tmp_path: Path) -> None:
    """Task PDF'i sayfa sayfa okuyup chunk sayısını raporlamalı ve dosyayı temizlemelidir."""
    sample = tmp_path / "sample.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "CoreRAG page one sample content for testing.")
        doc.save(sample)
    expected_size = sample.stat().st_size

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
        "size_bytes": expected_size,
        "page_count": 1,
        "chunk_count": 1,
        "upserted_count": 1,
        "status": "processed",
    }
    assert not sample.exists()

    missing_result = process_document_task(
        document_id="doc-2",
        file_path=str(tmp_path / "missing.pdf"),
        tenant_id="tenant-a",
        file_type="pdf",
    )
    assert missing_result == {
        "document_id": "doc-2",
        "tenant_id": "tenant-a",
        "file_type": "pdf",
        "file_exists": False,
        "size_bytes": 0,
        "page_count": 0,
        "chunk_count": 0,
        "upserted_count": 0,
        "status": "missing_file",
    }


def test_process_document_task_preserves_file_on_retry_and_cleans_up_on_final_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geçici hatada retry öncesi dosya korunmalı, son denemede diskten silinmelidir."""
    sample = tmp_path / "retry.txt"
    sample.write_text("temporary content", encoding="utf-8")

    def _always_fail(**kwargs: object) -> object:
        raise TransientProcessingError("temporary glitch")

    monkeypatch.setattr(document_tasks, "parse_and_chunk_document", _always_fail)

    process_document_task.push_request(retries=0)
    try:
        with pytest.raises((Retry, TransientProcessingError)):
            process_document_task(
                document_id="doc-retry",
                file_path=str(sample),
                tenant_id="tenant-a",
                file_type="txt",
            )
    finally:
        process_document_task.pop_request()

    assert sample.exists()

    process_document_task.push_request(retries=3)
    try:
        with pytest.raises(TransientProcessingError):
            process_document_task(
                document_id="doc-retry",
                file_path=str(sample),
                tenant_id="tenant-a",
                file_type="txt",
            )
    finally:
        process_document_task.pop_request()

    assert not sample.exists()


def test_process_document_task_propagates_and_resets_request_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Worker task verilen request_id değerini bağlamına alıp iş bitiminde sıfırlamalıdır."""
    seen_request_ids: list[str] = []

    class _ContextCapturer(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen_request_ids.append(request_id_ctx.get())

    task_logger = logging.getLogger("src.tasks.document_tasks")
    capturer = _ContextCapturer()
    task_logger.addHandler(capturer)
    try:
        with caplog.at_level(logging.INFO, logger="src.tasks.document_tasks"):
            process_document_task(
                document_id="doc-trace",
                file_path=str(tmp_path / "missing.pdf"),
                tenant_id="tenant-a",
                file_type="pdf",
                request_id="worker-req-77",
            )
    finally:
        task_logger.removeHandler(capturer)

    assert seen_request_ids == ["worker-req-77"]
    assert request_id_ctx.get() == "-"
    assert any("document_id=doc-trace" in record.message for record in caplog.records)


async def test_celery_document_task_dispatcher_calls_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CeleryDocumentTaskDispatcher tüm alanları delay() metoduna aktarıp task_id dönmelidir."""
    recorded_kwargs: dict[str, object] = {}

    def _fake_delay(**kwargs: object) -> SimpleNamespace:
        recorded_kwargs.update(kwargs)
        return SimpleNamespace(id="celery-task-999")

    monkeypatch.setattr(process_document_task, "delay", _fake_delay)

    dispatcher = CeleryDocumentTaskDispatcher()
    target = tmp_path / "doc.pdf"
    task_id = await dispatcher.enqueue_document(
        document_id="doc-9",
        file_path=target,
        tenant_id="tenant-z",
        file_type="pdf",
        request_id="req-999",
    )

    assert task_id == "celery-task-999"
    assert recorded_kwargs == {
        "document_id": "doc-9",
        "file_path": str(target),
        "tenant_id": "tenant-z",
        "file_type": "pdf",
        "request_id": "req-999",
    }


async def test_celery_document_task_dispatcher_get_task_status_maps_states_and_sanitizes_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncResult durumlarını normalize etmeli ve iç hata mesajlarını sızdırmamalıdır."""
    fake_results: dict[str, SimpleNamespace] = {
        "t-received": SimpleNamespace(state="RECEIVED", result=None),
        "t-started": SimpleNamespace(state="STARTED", result=None),
        "t-retry": SimpleNamespace(state="RETRY", result=None),
        "t-dict": SimpleNamespace(
            state="SUCCESS",
            result={"document_id": "d-1", "status": "processed_stub"},
        ),
        "t-scalar": SimpleNamespace(state="SUCCESS", result="ok"),
        "t-fail": SimpleNamespace(
            state="FAILURE",
            result=RuntimeError("secret-redis-password-leak"),
        ),
    }

    monkeypatch.setattr(celery_app, "AsyncResult", lambda task_id: fake_results[task_id])

    dispatcher = CeleryDocumentTaskDispatcher()

    pending_snap = await dispatcher.get_task_status(task_id="t-received")
    assert pending_snap.status == "PENDING"
    assert pending_snap.result is None

    assert (await dispatcher.get_task_status(task_id="t-started")).status == "STARTED"
    assert (await dispatcher.get_task_status(task_id="t-retry")).status == "RETRY"

    dict_snap = await dispatcher.get_task_status(task_id="t-dict")
    assert dict_snap.status == "SUCCESS"
    assert dict_snap.result == {"document_id": "d-1", "status": "processed_stub"}
    assert dict_snap.error is None

    scalar_snap = await dispatcher.get_task_status(task_id="t-scalar")
    assert scalar_snap.status == "SUCCESS"
    assert scalar_snap.result == {"value": "ok"}

    fail_snap = await dispatcher.get_task_status(task_id="t-fail")
    assert fail_snap.status == "FAILURE"
    assert fail_snap.result is None
    assert fail_snap.error == "Document processing failed"
