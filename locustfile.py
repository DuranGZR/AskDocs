"""CoreRAG-Engine eşzamanlı yük, gecikme (p50/p95/p99) ve dayanıklılık testi için Locust senaryosu.

Kullanım örnekleri:
    uv run locust -f locustfile.py --host http://localhost:8000 --api-key "$env:API_KEY"
    uv run locust -f locustfile.py --headless -u 20 -r 5 -t 45s --host http://localhost:8000
"""

from __future__ import annotations

import os
import random
import time
from functools import lru_cache
from typing import Any

import pymupdf
from locust import HttpUser, between, events, tag, task

_SAMPLE_QUERIES: tuple[str, ...] = (
    "Yıllık ücretli izin hakkı kaç gündür ve uzaktan çalışma kuralları nelerdir?",
    "Bitcoin whitepaper belgesinde çift harcama sorunu nasıl çözülüyor?",
    "RAG yaklaşımında parametrik ve parametrik olmayan bellek nasıl birleştirilir?",
    "Asenkron doküman işleme kuyruğu ve Qdrant tenant izolasyonu nasıl çalışır?",
)


@events.init_command_line_parser.add_listener
def register_corerag_cli_options(parser: Any) -> None:
    """Locust CLI ve Web UI için CoreRAG'e özel kimlik ve test seçeneklerini kaydeder."""
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("CORERAG_API_KEY") or os.environ.get("API_KEY", ""),
        env_var="CORERAG_API_KEY",
        is_secret=True,
        help="Korumalı uç noktalar için X-API-Key değeri (varsayılan: env CORERAG_API_KEY/API_KEY)",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=os.environ.get("CORERAG_LOAD_TENANT_ID", "load_test_tenant"),
        env_var="CORERAG_LOAD_TENANT_ID",
        help="Yük testi boyunca kullanılacak izole tenant_id",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=20.0,
        help="Yüklenen dokümanın worker tarafından indekslenmesi için maksimum bekleme süresi (sn)",
    )
    parser.add_argument(
        "--accept-429",
        action="store_true",
        default=False,
        help="Aktifse Redis hız sınırı (429 Too Many Requests) yanıtlarını beklenen davranış sayar",
    )


@lru_cache(maxsize=1)
def build_sample_pdf_bytes() -> bytes:
    """Geçerli %PDF- imzalı ve çıkarılabilir metin içeren deterministik örnek PDF üretir."""
    with pymupdf.open() as doc:
        rect = pymupdf.Rect(48, 48, 540, 760)
        for page_num in (1, 2):
            page = doc.new_page()
            lines = [
                f"CoreRAG-Engine Yük Testi Referans Dokümanı — Sayfa {page_num}.",
                "Çalışanların yıllık ücretli izin hakkı kıdeme göre yılda en az 20 iş günüdür.",
                "Haftada 2 gün uzaktan çalışma ve şirket içi VPN güvenlik kuralları uygulanır.",
                "Qdrant vektör veritabanında tüm parçalar tenant_id filtresiyle izole tutulur.",
            ]
            page.insert_textbox(rect, "\n".join(lines))
        return bytes(doc.tobytes())


class CoreRAGUser(HttpUser):
    """Gerçekçi okuma, sorgulama, SSE akışı ve doküman yaşam döngüsü yürüten kullanıcı."""

    wait_time = between(0.2, 0.7)

    def on_start(self) -> None:
        opts = self.environment.parsed_options
        self._api_key = str(getattr(opts, "api_key", "") or "").strip()
        self._tenant_id = str(getattr(opts, "tenant_id", "load_test_tenant") or "load_test_tenant")
        self._poll_timeout = float(getattr(opts, "poll_timeout", 20.0) or 20.0)
        self._accept_429 = bool(getattr(opts, "accept_429", False))
        self._pdf_bytes = build_sample_pdf_bytes()

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"X-API-Key": self._api_key}

    def _validate_status(self, response: Any, *, expected_codes: tuple[int, ...]) -> bool:
        if response.status_code in expected_codes:
            return True
        if response.status_code == 429 and self._accept_429:
            response.success()
            return False
        if response.status_code == 401:
            response.failure("401 Unauthorized: geçerli --api-key veya API_KEY sağlanmalı")
            return False
        response.failure(f"Beklenmeyen HTTP durum kodu: {response.status_code}")
        return False

    @tag("health")
    @task(2)
    def health_check(self) -> None:
        """Sistem bağımlılık sağlık kontrolünü (storage, redis, qdrant) yoklar."""
        with self.client.get(
            "/api/v1/health",
            name="GET /api/v1/health",
            catch_response=True,
        ) as resp:
            if not self._validate_status(resp, expected_codes=(200,)):
                return
            try:
                body = resp.json()
            except ValueError as exc:
                resp.failure(f"Geçersiz health JSON yanıtı: {exc}")
                return
            if body.get("status") != "ok":
                resp.failure(f"Health degraded durumu: {body.get('checks')}")
                return
            resp.success()

    @tag("read", "documents")
    @task(4)
    def list_documents(self) -> None:
        """Tenant altındaki indekslenmiş doküman listesini Qdrant üzerinden çeker."""
        with self.client.get(
            "/api/v1/documents",
            params={"tenant_id": self._tenant_id},
            headers=self._auth_headers(),
            name="GET /api/v1/documents",
            catch_response=True,
        ) as resp:
            if not self._validate_status(resp, expected_codes=(200,)):
                return
            try:
                body = resp.json()
            except ValueError as exc:
                resp.failure(f"Geçersiz documents JSON yanıtı: {exc}")
                return
            if "documents" not in body or not isinstance(body["documents"], list):
                resp.failure("documents listesi yanıt gövdesinde bulunamadı")
                return
            resp.success()

    @tag("read", "tenants")
    @task(2)
    def list_tenants(self) -> None:
        """Bilinen çalışma alanı (tenant) listesini sorgular."""
        with self.client.get(
            "/api/v1/documents/tenants",
            headers=self._auth_headers(),
            name="GET /api/v1/documents/tenants",
            catch_response=True,
        ) as resp:
            if not self._validate_status(resp, expected_codes=(200,)):
                return
            try:
                body = resp.json()
            except ValueError as exc:
                resp.failure(f"Geçersiz tenants JSON yanıtı: {exc}")
                return
            if "tenants" not in body or not isinstance(body["tenants"], list):
                resp.failure("tenants listesi yanıt gövdesinde bulunamadı")
                return
            resp.success()

    @tag("query", "rag")
    @task(4)
    def query_json(self) -> None:
        """Senkron JSON RAG sorgusuyla embedding + Qdrant araması + LLM yanıtını ölçer."""
        query = random.choice(_SAMPLE_QUERIES)
        with self.client.post(
            "/api/v1/query",
            json={
                "query": query,
                "tenant_id": self._tenant_id,
                "top_k": 5,
                "stream": False,
            },
            headers=self._auth_headers(),
            name="POST /api/v1/query",
            catch_response=True,
        ) as resp:
            if not self._validate_status(resp, expected_codes=(200,)):
                return
            try:
                body = resp.json()
            except ValueError as exc:
                resp.failure(f"Geçersiz query JSON yanıtı: {exc}")
                return
            if not isinstance(body.get("answer"), str) or not isinstance(
                body.get("citations"), list
            ):
                resp.failure("Query yanıtında answer veya citations alanı eksik")
                return
            resp.success()

    @tag("query", "stream", "rag")
    @task(3)
    def query_sse_stream(self) -> None:
        """SSE akışlı RAG sorgusunu başlangıçtan [DONE] işaretine kadar tüketip doğrular."""
        query = random.choice(_SAMPLE_QUERIES)
        with self.client.post(
            "/api/v1/query/stream",
            json={
                "query": query,
                "tenant_id": self._tenant_id,
                "top_k": 5,
                "stream": True,
            },
            headers=self._auth_headers(),
            name="POST /api/v1/query/stream",
            stream=True,
            catch_response=True,
        ) as resp:
            if not self._validate_status(resp, expected_codes=(200,)):
                return
            saw_citations = False
            saw_done = False
            saw_error = False
            current_event = ""

            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = str(raw_line).strip()
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    if current_event == "citations":
                        saw_citations = True
                    elif current_event == "error":
                        saw_error = True
                elif line.startswith("data:") and line[5:].strip() == "[DONE]":
                    saw_done = True
                    break

            if saw_error:
                resp.failure("SSE akışı içinde event: error çerçevesi döndü")
                return
            if not saw_citations or not saw_done:
                resp.failure(f"Eksik SSE çerçevesi (citations={saw_citations}, done={saw_done})")
                return
            resp.success()

    @tag("write", "upload", "pipeline")
    @task(1)
    def upload_poll_and_delete(self) -> None:
        """PDF yükleme -> Celery görev takibi -> Qdrant temizleme yaşam döngüsünü koşar."""
        document_id: str | None = None
        task_id: str | None = None

        with self.client.post(
            "/api/v1/documents/upload",
            data={"tenant_id": self._tenant_id},
            files={"file": ("load_probe.pdf", self._pdf_bytes, "application/pdf")},
            headers=self._auth_headers(),
            name="POST /api/v1/documents/upload",
            catch_response=True,
        ) as upload_resp:
            if not self._validate_status(upload_resp, expected_codes=(202,)):
                return
            try:
                body = upload_resp.json()
                document_id = str(body["document_id"])
                task_id = str(body["task_id"])
            except (ValueError, KeyError) as exc:
                upload_resp.failure(f"Geçersiz upload 202 yanıtı: {exc}")
                return
            upload_resp.success()

        if task_id:
            poll_started = time.perf_counter()
            poll_completed = False
            poll_failed = False
            deadline = time.monotonic() + self._poll_timeout
            while time.monotonic() < deadline:
                with self.client.get(
                    f"/api/v1/documents/status/{task_id}",
                    headers=self._auth_headers(),
                    name="GET /api/v1/documents/status/[task_id]",
                    catch_response=True,
                ) as status_resp:
                    if not self._validate_status(status_resp, expected_codes=(200,)):
                        poll_failed = True
                        break
                    try:
                        snapshot = status_resp.json()
                    except ValueError as exc:
                        status_resp.failure(f"Geçersiz status JSON yanıtı: {exc}")
                        poll_failed = True
                        break
                    task_state = snapshot.get("status")
                    if task_state == "SUCCESS":
                        status_resp.success()
                        poll_completed = True
                        break
                    if task_state == "FAILURE":
                        status_resp.failure(
                            f"Worker görevi başarısız oldu: {snapshot.get('error')}"
                        )
                        poll_failed = True
                        break
                    status_resp.success()
                time.sleep(0.25)

            if not poll_completed and not poll_failed:
                elapsed_ms = (time.perf_counter() - poll_started) * 1000.0
                self.environment.events.request.fire(
                    request_type="POLL",
                    name="GET /api/v1/documents/status/[task_id]",
                    response_time=elapsed_ms,
                    response_length=0,
                    exception=TimeoutError(
                        f"Task {task_id} did not reach SUCCESS within {self._poll_timeout}s"
                    ),
                )

        if document_id:
            with self.client.delete(
                f"/api/v1/documents/{document_id}",
                params={"tenant_id": self._tenant_id},
                headers=self._auth_headers(),
                name="DELETE /api/v1/documents/[document_id]",
                catch_response=True,
            ) as del_resp:
                if self._validate_status(del_resp, expected_codes=(200,)):
                    del_resp.success()
