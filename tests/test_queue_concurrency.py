import asyncio
import time
from pathlib import Path
from typing import Any

import celery.app.autoretry as celery_autoretry
import httpx
import pymupdf
import pytest
from fastapi import FastAPI

from src.api.dependencies import get_task_dispatcher
from src.core.config import Settings
from src.services.base import TaskStatusSnapshot
from src.services.pdf_parser import TransientProcessingError
from src.tasks import document_tasks
from src.tasks.document_tasks import process_document_task


def _build_pdf_bytes(index: int, page_count: int = 2) -> bytes:
    """Eşzamanlı testler için gerçek çok sayfalı PDF baytları üretir."""
    with pymupdf.open() as doc:
        rect = pymupdf.Rect(50, 50, 540, 750)
        for page_num in range(1, page_count + 1):
            page = doc.new_page()
            content = f"Belge #{index}, Sayfa {page_num}. " + " ".join(
                f"Kayıt {i} sıralı worker testi." for i in range(1, 10)
            )
            page.insert_textbox(rect, content)
        return bytes(doc.tobytes())


class _QueuedWorkerDispatcher:
    """Eşzamanlı yüklenen görevleri kuyrukta toplayıp sırayla işleyen test dispatcher'ı."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self.snapshots: dict[str, TaskStatusSnapshot] = {}
        self.execution_windows: list[tuple[str, float, float]] = []

    async def enqueue_document(
        self,
        *,
        document_id: str,
        file_path: Path,
        tenant_id: str,
        file_type: str,
        request_id: str,
    ) -> str:
        await asyncio.sleep(0.005)
        task_id = f"queue-task-{document_id}"
        self.snapshots[task_id] = TaskStatusSnapshot(task_id=task_id, status="PENDING")
        await self._queue.put(
            {
                "task_id": task_id,
                "document_id": document_id,
                "file_path": str(file_path),
                "tenant_id": tenant_id,
                "file_type": file_type,
                "request_id": request_id,
            }
        )
        return task_id

    async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
        return self.snapshots.get(task_id, TaskStatusSnapshot(task_id=task_id, status="PENDING"))

    async def process_all_sequentially(self, expected_count: int) -> None:
        """Kuyruktaki görevleri worker_prefetch_multiplier=1 mantığıyla tek tek çalıştırır."""
        for _ in range(expected_count):
            job = await self._queue.get()
            task_id = job["task_id"]
            self.snapshots[task_id] = TaskStatusSnapshot(task_id=task_id, status="STARTED")

            started = time.perf_counter()
            eager_result = await asyncio.to_thread(
                process_document_task.apply,
                kwargs={
                    "document_id": job["document_id"],
                    "file_path": job["file_path"],
                    "tenant_id": job["tenant_id"],
                    "file_type": job["file_type"],
                    "request_id": job["request_id"],
                },
            )
            finished = time.perf_counter()
            self.execution_windows.append((task_id, started, finished))

            if eager_result.state == "SUCCESS" and isinstance(eager_result.result, dict):
                self.snapshots[task_id] = TaskStatusSnapshot(
                    task_id=task_id,
                    status="SUCCESS",
                    result=dict(eager_result.result),
                )
            else:
                self.snapshots[task_id] = TaskStatusSnapshot(
                    task_id=task_id,
                    status="FAILURE",
                    error="Document processing failed",
                )
            self._queue.task_done()


def _prepare_upload_inputs(total: int) -> list[tuple[str, str, bytes, str]]:
    """PDF ve TXT karışımı yükleme girdileri listesi üretir."""
    items: list[tuple[str, str, bytes, str]] = []
    for idx in range(1, total + 1):
        tenant_id = f"tenant_{(idx % 4) + 1}"
        if idx % 5 == 0:
            txt_bytes = f"Metin dokümanı #{idx}. Asenkron kuyruk içeriği.".encode()
            items.append((tenant_id, f"notes_{idx}.txt", txt_bytes, "text/plain"))
        else:
            items.append(
                (tenant_id, f"contract_{idx}.pdf", _build_pdf_bytes(idx), "application/pdf")
            )
    return items


async def test_concurrent_uploads_do_not_block_api_or_health_endpoint(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """25 eşzamanlı yükleme API event loop'unu veya /health ucunu bloklamamalıdır."""
    dispatcher = _QueuedWorkerDispatcher()
    app.dependency_overrides[get_task_dispatcher] = lambda: dispatcher

    inputs = _prepare_upload_inputs(25)
    upload_coros = [
        client.post(
            "/api/v1/documents/upload",
            data={"tenant_id": tenant_id},
            files={"file": (filename, payload, content_type)},
        )
        for tenant_id, filename, payload, content_type in inputs
    ]
    health_coros = [client.get("/api/v1/health") for _ in range(5)]

    all_responses = await asyncio.gather(*upload_coros, *health_coros)
    upload_responses = all_responses[:25]
    health_responses = all_responses[25:]

    assert all(resp.status_code == 202 for resp in upload_responses)
    assert all(resp.json()["status"] == "queued" for resp in upload_responses)
    document_ids = {resp.json()["document_id"] for resp in upload_responses}
    task_ids = {resp.json()["task_id"] for resp in upload_responses}
    assert len(document_ids) == 25
    assert len(task_ids) == 25

    assert all(resp.status_code == 200 for resp in health_responses)
    assert all(resp.json()["status"] == "ok" for resp in health_responses)


async def test_worker_processes_queued_documents_sequentially_and_cleans_up_files(
    app: FastAPI,
    client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    """Worker kuyruktaki 25 görevi çakışmadan sırayla işlemeli ve dosyaları silmelidir."""
    dispatcher = _QueuedWorkerDispatcher()
    app.dependency_overrides[get_task_dispatcher] = lambda: dispatcher

    inputs = _prepare_upload_inputs(25)
    upload_responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/documents/upload",
                data={"tenant_id": tenant_id},
                files={"file": (filename, payload, content_type)},
            )
            for tenant_id, filename, payload, content_type in inputs
        )
    )
    assert len(list(test_settings.UPLOAD_DIR.iterdir())) == 25

    task_ids = [resp.json()["task_id"] for resp in upload_responses]
    await dispatcher.process_all_sequentially(25)

    assert len(dispatcher.execution_windows) == 25
    for prev, curr in zip(
        dispatcher.execution_windows,
        dispatcher.execution_windows[1:],
        strict=False,
    ):
        # Sıralı tüketimde sonraki görev bir önceki bitmeden başlamamalıdır
        assert curr[1] >= prev[2]

    status_responses = await asyncio.gather(
        *(client.get(f"/api/v1/documents/status/{task_id}") for task_id in task_ids)
    )
    for resp in status_responses:
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["result"]["status"] == "processed"
        assert body["result"]["page_count"] >= 1
        assert body["result"]["chunk_count"] >= 1

    assert list(test_settings.UPLOAD_DIR.iterdir()) == []


def test_transient_failures_retry_with_exponential_backoff_until_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geçici hata veren görev üstel backoff ile tekrar denenip başarıda dosyayı silmelidir."""
    target = tmp_path / "transient-recovery.txt"
    target.write_text("Tekrar deneme sonrası başarıyla işlenecek içerik.", encoding="utf-8")

    orig_parser = document_tasks.parse_and_chunk_document
    orig_backoff = celery_autoretry.get_exponential_backoff_interval

    attempts = 0
    backoff_records: list[tuple[int, int, bool, bool]] = []

    def _fail_twice_then_succeed(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise TransientProcessingError(f"transient outage #{attempts}")
        return orig_parser(**kwargs)

    def _record_backoff(
        factor: int,
        retries: int,
        maximum: int,
        full_jitter: bool = False,
    ) -> int:
        upper_bound = min(maximum, factor * (2**retries))
        backoff_records.append((retries, upper_bound, full_jitter, target.exists()))
        return int(
            orig_backoff(
                factor=factor,
                retries=retries,
                maximum=maximum,
                full_jitter=full_jitter,
            )
        )

    monkeypatch.setattr(document_tasks, "parse_and_chunk_document", _fail_twice_then_succeed)
    monkeypatch.setattr(celery_autoretry, "get_exponential_backoff_interval", _record_backoff)

    eager_result = process_document_task.apply(
        kwargs={
            "document_id": "doc-backoff-1",
            "file_path": str(target),
            "tenant_id": "tenant-retry",
            "file_type": "txt",
            "request_id": "req-backoff-1",
        }
    )

    assert eager_result.state == "SUCCESS"
    assert eager_result.result["status"] == "processed"
    assert attempts == 3
    assert backoff_records == [
        (0, 1, True, True),
        (1, 2, True, True),
    ]
    assert not target.exists()
