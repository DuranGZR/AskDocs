"""Hafta 2 (10. Gün) Kuyruk, Eşzamanlılık ve Backoff Retry doğrulama script'i.

Varsayılan modda dış servis gerektirmeden FastAPI ASGITransport + gerçek PyMuPDF +
sıralı worker simülasyonu ve Celery autoretry mekanizmasını uçtan uca doğrular.
Canlı Docker Compose/sunucu ortamında çalıştırmak için ``--base-url`` verilebilir.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pymupdf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if TYPE_CHECKING:
    from src.services.base import TaskStatusSnapshot, VectorPoint


@dataclass(frozen=True, slots=True)
class _RetryObservation:
    """Celery backoff ve dosya koruma gözlem kaydı."""

    retries: int
    upper_bound_seconds: int
    countdown_seconds: int
    full_jitter: bool
    file_preserved: bool


def _build_pdf_bytes(doc_index: int, page_count: int = 2) -> bytes:
    """Test için geçerli çok sayfalı gerçek PDF baytları üretir."""
    with pymupdf.open() as doc:
        rect = pymupdf.Rect(50, 50, 540, 750)
        for page_idx in range(1, page_count + 1):
            page = doc.new_page()
            text = f"CoreRAG-Engine Doküman #{doc_index}, Sayfa {page_idx}. " + " ".join(
                f"Madde {i}: Eşzamanlı kuyruk işleme ve parçalama doğrulama cümlesi."
                for i in range(1, 12)
            )
            page.insert_textbox(rect, text)
        return bytes(doc.tobytes())


def _build_upload_payload(index: int) -> tuple[str, str, bytes, str]:
    """PDF ve TXT karışımı deterministik test dokümanı üretir."""
    tenant_id = f"tenant_{(index % 3) + 1}"
    if index % 4 == 0:
        content = (
            f"CoreRAG TXT Belgesi #{index}.\n"
            + " ".join(f"Satır {n}: Asenkron kuyruk worker doğrulama metni." for n in range(1, 20))
        ).encode("utf-8")
        return tenant_id, f"doc_{index}.txt", content, "text/plain"

    pdf_bytes = _build_pdf_bytes(index, page_count=2)
    return tenant_id, f"doc_{index}.pdf", pdf_bytes, "application/pdf"


@contextmanager
def _temporary_env(overrides: dict[str, str]) -> Iterator[None]:
    """Geçici ortam değişkenlerini ayarlar ve blok çıkışında eski haline getirir."""
    previous: dict[str, str | None] = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


class _SequentialQueueSimulator:
    """Celery + Redis kuyruğunu tekil sıralı worker davranışıyla simüle eder."""

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
        from src.services.base import TaskStatusSnapshot

        await asyncio.sleep(0.002)
        task_id = f"sim-task-{document_id}"
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
        from src.services.base import TaskStatusSnapshot

        return self.snapshots.get(
            task_id,
            TaskStatusSnapshot(task_id=task_id, status="PENDING"),
        )

    async def drain_sequentially(self, expected_count: int) -> None:
        """Kuyruktaki tüm görevleri tek tek (overlap olmadan) çalıştırır."""
        from src.services.base import TaskStatusSnapshot
        from src.tasks.document_tasks import process_document_task

        for _ in range(expected_count):
            job = await self._queue.get()
            task_id = job["task_id"]
            self.snapshots[task_id] = TaskStatusSnapshot(task_id=task_id, status="STARTED")

            started_at = time.perf_counter()
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
            finished_at = time.perf_counter()
            self.execution_windows.append((task_id, started_at, finished_at))

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


def _verify_retry_backoff(upload_dir: Path) -> list[_RetryObservation]:
    """Celery autoretry + exponential backoff + dosya koruma/silme davranışını doğrular."""
    import celery.app.autoretry as celery_autoretry

    from src.services.pdf_parser import TransientProcessingError
    from src.tasks import document_tasks
    from src.tasks.document_tasks import process_document_task

    retry_sample = upload_dir / "retry-backoff-check.txt"
    retry_sample.write_text("Geçici hata sonrası backoff kurtarma metni.", encoding="utf-8")

    orig_parser = document_tasks.parse_and_chunk_document
    orig_backoff = celery_autoretry.get_exponential_backoff_interval

    attempts = 0
    observations: list[_RetryObservation] = []

    def _flaky_parser(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise TransientProcessingError(f"Simulated outage attempt #{attempts}")
        return orig_parser(**kwargs)

    def _spy_backoff(
        factor: int,
        retries: int,
        maximum: int,
        full_jitter: bool = False,
    ) -> int:
        countdown = int(
            orig_backoff(factor=factor, retries=retries, maximum=maximum, full_jitter=full_jitter)
        )
        upper_bound = min(maximum, factor * (2**retries))
        observations.append(
            _RetryObservation(
                retries=retries,
                upper_bound_seconds=upper_bound,
                countdown_seconds=countdown,
                full_jitter=full_jitter,
                file_preserved=retry_sample.exists(),
            )
        )
        return countdown

    document_tasks.parse_and_chunk_document = _flaky_parser  # type: ignore[assignment]
    celery_autoretry.get_exponential_backoff_interval = _spy_backoff
    try:
        eager_result = process_document_task.apply(
            kwargs={
                "document_id": "doc-backoff-check",
                "file_path": str(retry_sample),
                "tenant_id": "tenant_retry",
                "file_type": "txt",
                "request_id": "req-backoff-check",
            }
        )
    finally:
        document_tasks.parse_and_chunk_document = orig_parser
        celery_autoretry.get_exponential_backoff_interval = orig_backoff

    if eager_result.state != "SUCCESS":
        raise AssertionError(f"Retry task failed unexpectedly: state={eager_result.state}")
    if (
        not isinstance(eager_result.result, dict)
        or eager_result.result.get("status") != "processed"
    ):
        raise AssertionError(f"Unexpected retry task result: {eager_result.result}")
    if attempts != 3:
        raise AssertionError(f"Expected 3 total attempts (2 retries), got {attempts}")
    if [obs.upper_bound_seconds for obs in observations] != [1, 2]:
        raise AssertionError(f"Unexpected exponential backoff caps: {observations}")
    if not all(obs.file_preserved and obs.full_jitter for obs in observations):
        raise AssertionError(f"Temp file was not preserved or jitter was disabled: {observations}")
    if retry_sample.exists():
        raise AssertionError("Temp file was not cleaned up after retry succeeded")

    return observations


async def _upload_one(
    client: httpx.AsyncClient,
    index: int,
) -> tuple[dict[str, Any], float]:
    """Tek bir dokümanı upload uç noktasına gönderip yanıtı ve süreyi döner."""
    tenant_id, filename, payload, content_type = _build_upload_payload(index)
    started = time.perf_counter()
    response = await client.post(
        "/api/v1/documents/upload",
        data={"tenant_id": tenant_id},
        files={"file": (filename, payload, content_type)},
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if response.status_code != 202:
        raise AssertionError(
            f"Upload #{index} failed with status={response.status_code}: {response.text}"
        )
    data = dict(response.json())
    if data.get("status") != "queued":
        raise AssertionError(f"Upload #{index} did not return status='queued': {data}")
    return data, elapsed_ms


async def _probe_health(client: httpx.AsyncClient) -> float:
    """API /health uç noktasının bloklanmadan 200 OK döndüğünü ölçer."""
    started = time.perf_counter()
    response = await client.get("/api/v1/health")
    elapsed_ms = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        raise AssertionError(f"Health probe failed with status={response.status_code}")
    return elapsed_ms


async def _run_offline_suite(count: int) -> None:
    """Dış bağımlılık gerektirmeden bellek içi tam concurrency & retry doğrulaması yapar."""
    with tempfile.TemporaryDirectory(prefix="corerag-concurrency-") as tmp_dir:
        upload_dir = Path(tmp_dir) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        api_key = os.environ.get("API_KEY") or "concurrency-test-key-1234"
        env_overrides = {
            "API_KEY": api_key,
            "RATE_LIMIT_ENABLED": "false",
            "LANGFUSE_ENABLED": "false",
            "UPLOAD_DIR": str(upload_dir),
            "APP_ENV": "development",
            "LOG_LEVEL": "WARNING",
        }
        with _temporary_env(env_overrides):
            from src.api.dependencies import get_task_dispatcher
            from src.core.config import Settings, get_settings
            from src.main import create_app
            from src.services.base import SyncBaseLLMClient, SyncBaseVectorDB
            from src.tasks import document_tasks

            class _OfflineSyncLLMClient(SyncBaseLLMClient):
                def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
                    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

                def close(self) -> None:
                    return None

            class _OfflineSyncVectorDB(SyncBaseVectorDB):
                def ensure_collection(self) -> None:
                    return None

                def upsert(self, *, points: Sequence[VectorPoint]) -> int:
                    return len(points)

                def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
                    return None

                def close(self) -> None:
                    return None

            def _offline_llm_factory(settings: Settings) -> SyncBaseLLMClient:
                del settings
                return _OfflineSyncLLMClient()

            def _offline_vdb_factory(settings: Settings) -> SyncBaseVectorDB:
                del settings
                return _OfflineSyncVectorDB()

            orig_llm_factory = document_tasks.create_worker_llm_client
            orig_vdb_factory = document_tasks.create_worker_vector_db
            document_tasks.create_worker_llm_client = _offline_llm_factory
            document_tasks.create_worker_vector_db = _offline_vdb_factory

            get_settings.cache_clear()
            app = create_app()
            simulator = _SequentialQueueSimulator()
            app.dependency_overrides[get_task_dispatcher] = lambda: simulator

            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"X-API-Key": api_key},
                ) as client:
                    # 1. Eşzamanlı yükleme + aktif worker + sağlık yoklaması (API bloklanmaz)
                    worker_task = asyncio.create_task(simulator.drain_sequentially(count))
                    upload_tasks = [_upload_one(client, idx) for idx in range(1, count + 1)]
                    health_tasks = [_probe_health(client) for _ in range(5)]

                    upload_results = await asyncio.gather(*upload_tasks)
                    health_latencies = await asyncio.gather(*health_tasks)
                    await worker_task

                    task_ids = [item[0]["task_id"] for item in upload_results]
                    upload_latencies = [item[1] for item in upload_results]
                    if len(set(task_ids)) != count:
                        raise AssertionError("Duplicate task_id detected across concurrent uploads")

                    # 2. Sıralı worker yürütme ve status uç noktası doğrulaması
                    for prev, curr in zip(
                        simulator.execution_windows,
                        simulator.execution_windows[1:],
                        strict=False,
                    ):
                        if curr[1] < prev[2]:
                            raise AssertionError(
                                f"Sequential worker invariant violated: {prev} overlapped {curr}"
                            )

                    status_responses = await asyncio.gather(
                        *(client.get(f"/api/v1/documents/status/{tid}") for tid in task_ids)
                    )
                    for resp in status_responses:
                        if resp.status_code != 200:
                            raise AssertionError(f"Status endpoint error: {resp.status_code}")
                        body = resp.json()
                        if body["status"] != "SUCCESS":
                            raise AssertionError(f"Task did not finish cleanly: {body}")
                        if body["result"]["status"] != "processed":
                            raise AssertionError(f"Unexpected task payload: {body}")

                leftover_files = list(upload_dir.iterdir())
                if leftover_files:
                    raise AssertionError(f"Temporary upload files leaked on disk: {leftover_files}")

                # 3. Geçici hata -> exponential backoff -> kurtarma & dosya temizliği doğrulaması
                retry_observations = _verify_retry_backoff(upload_dir)

                print(
                    f"[PASS] Concurrent uploads ({count} files): "
                    f"all 202 queued, max={max(upload_latencies):.1f}ms"
                )
                print(
                    "[PASS] Non-blocking API health probes (5 calls): "
                    f"all 200 OK, max={max(health_latencies):.1f}ms"
                )
                print(
                    "[PASS] Sequential worker processing: "
                    f"{len(simulator.execution_windows)} tasks, 0 overlaps, 0 leaked temp files"
                )
                backoff_summary = ", ".join(
                    f"retry#{obs.retries}: cap={obs.upper_bound_seconds}s "
                    f"countdown={obs.countdown_seconds}s"
                    for obs in retry_observations
                )
                print(f"[PASS] Exponential backoff & cleanup: {backoff_summary}")
            finally:
                document_tasks.create_worker_llm_client = orig_llm_factory
                document_tasks.create_worker_vector_db = orig_vdb_factory
                app.dependency_overrides.clear()
                get_settings.cache_clear()


async def _run_live_suite(
    base_url: str,
    count: int,
    timeout_seconds: float,
    api_key: str | None = None,
) -> None:
    """Çalışan HTTP API + Redis + Celery worker yığınına karşı canlı yük/durum testi koşar."""
    resolved_key = api_key or os.environ.get("API_KEY", "")
    headers = {"X-API-Key": resolved_key} if resolved_key else {}
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=30.0,
        headers=headers,
    ) as client:
        upload_results = await asyncio.gather(
            *(_upload_one(client, idx) for idx in range(1, count + 1))
        )
        health_latencies = await asyncio.gather(*(_probe_health(client) for _ in range(5)))

        pending_ids = {str(item[0]["task_id"]) for item in upload_results}
        completed: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout_seconds

        while pending_ids and time.monotonic() < deadline:
            responses = await asyncio.gather(
                *(client.get(f"/api/v1/documents/status/{tid}") for tid in list(pending_ids))
            )
            for tid, resp in zip(list(pending_ids), responses, strict=True):
                if resp.status_code != 200:
                    raise AssertionError(f"Status request failed for {tid}: {resp.status_code}")
                payload = dict(resp.json())
                if payload["status"] in {"SUCCESS", "FAILURE"}:
                    completed[tid] = payload
                    pending_ids.remove(tid)
            if pending_ids:
                await asyncio.sleep(0.5)

        if pending_ids:
            raise TimeoutError(f"{len(pending_ids)} tasks did not finish within {timeout_seconds}s")

        failed = [tid for tid, body in completed.items() if body["status"] == "FAILURE"]
        if failed:
            raise AssertionError(f"{len(failed)} tasks ended in FAILURE: {failed[:5]}")

        print(f"[PASS] Live concurrent uploads ({count} files): all 202 queued")
        print(f"[PASS] Live API health probes: all 200 OK, max={max(health_latencies):.1f}ms")
        print(f"[PASS] Live worker completion: {len(completed)}/{count} SUCCESS")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CoreRAG-Engine concurrent upload, sequential worker, and retry verifier",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=25,
        help="Eşzamanlı gönderilecek doküman sayısı (varsayılan: 25)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Canlı API adresi (örn. http://localhost:8000); boşsa bellek içi doğrulama koşar.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Canlı modda görevlerin tamamlanması için maksimum bekleme süresi (saniye)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Canlı mod için X-API-Key başlığı (boşsa ortam değişkeninden okunur)",
    )
    return parser.parse_args()


def main() -> int:
    """CLI giriş noktası."""
    args = _parse_args()
    if args.count < 1:
        raise ValueError("--count must be at least 1")

    if args.base_url:
        asyncio.run(_run_live_suite(args.base_url, args.count, args.timeout, args.api_key))
    else:
        asyncio.run(_run_offline_suite(args.count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
