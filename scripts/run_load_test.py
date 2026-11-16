"""Hafta 4 (20. Gün) Locust eşzamanlı yük testi, p50/p95/p99 gecikme ve konteyner RAM profili aracı.

Varsayılan olarak hedef sunucuda Locust yük testini koşar, arka planda ``docker stats`` ile
API/Worker/Qdrant/Redis RAM kullanımını örnekler ve OOM/sızıntı kontrolü raporlar.
``--with-mock-ai`` bayrağıyla dış LLM kotası harcamadan izole mock OpenAI sunucusu ve geçici
``corerag_loadtest`` Qdrant koleksiyonu üzerinde tam uçtan uca yük/bellek testi yürütür.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import get_settings  # noqa: E402
from src.evals.load_metrics import (  # noqa: E402
    ContainerMemorySample,
    parse_docker_stats_line,
    parse_locust_stats_csv,
    summarize_container_memory,
)

DEFAULT_CONTAINERS: tuple[str, ...] = (
    "corerag-api",
    "corerag-worker",
    "corerag-qdrant",
    "corerag-redis",
)
LOAD_TEST_COLLECTION = "corerag_loadtest"


_MOCK_AI_TEMPLATE = """import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VECTOR_SIZE = __VECTOR_SIZE__
BASE_VECTOR = [1.0] + [0.0] * max(0, VECTOR_SIZE - 1)


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", "0") or "0")
        body_raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        body = json.loads(body_raw.decode("utf-8"))

        if self.path.endswith("/embeddings"):
            raw_input = body.get("input", [])
            items = [raw_input] if isinstance(raw_input, str) else list(raw_input)
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": idx, "embedding": BASE_VECTOR}
                        for idx in range(len(items))
                    ],
                },
            )
            return

        if self.path.endswith("/chat/completions"):
            if bool(body.get("stream", False)):
                chunks = [
                    "Yıllık ",
                    "ücretli izin ",
                    "hakkı en az 20 iş günüdür ",
                    "[Source 1].",
                ]
                lines = [
                    "data: " + json.dumps({"choices": [{"delta": {"content": delta}}]})
                    for delta in chunks
                ]
                lines.append("data: [DONE]")
                payload = ("\\n\\n".join(lines) + "\\n\\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            self._send_json(
                200,
                {
                    "id": "mock-chatcmpl-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "Yıllık ücretli izin hakkı en az 20 iş günüdür [Source 1]."
                                ),
                            },
                        }
                    ],
                },
            )
            return

        self._send_json(404, {"error": "unsupported_endpoint"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 18080), _MockOpenAIHandler)
    server.daemon_threads = True
    server.request_queue_size = 128
    server.serve_forever()
"""


def build_mock_ai_server_source(vector_size: int) -> str:
    """Eşzamanlı yük testi için OpenAI-uyumlu minimal mock sunucu Python kodunu üretir."""
    if vector_size < 1:
        raise ValueError("vector_size must be at least 1")
    return _MOCK_AI_TEMPLATE.replace("__VECTOR_SIZE__", str(vector_size))


def sample_docker_stats(containers: Sequence[str]) -> list[ContainerMemorySample]:
    """Hedef konteynerlerden ``docker stats --no-stream`` ölçümü alır."""
    cmd = [
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}",
        *containers,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    samples: list[ContainerMemorySample] = []
    for line in proc.stdout.splitlines():
        parsed = parse_docker_stats_line(line)
        if parsed is not None:
            samples.append(parsed)
    return samples


def inspect_containers_oom_and_restarts(
    containers: Sequence[str],
) -> dict[str, tuple[bool, int]]:
    """Konteynerlerde OOMKilled ve yeniden başlama sayacını denetler."""
    results: dict[str, tuple[bool, int]] = {}
    for name in containers:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.OOMKilled}}\t{{.RestartCount}}",
                name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or "\t" not in proc.stdout:
            continue
        oom_str, restarts_str = proc.stdout.strip().split("\t", maxsplit=1)
        results[name] = (oom_str.strip().lower() == "true", int(restarts_str.strip() or "0"))
    return results


def _wait_for_api_healthy(base_url: str, *, timeout_seconds: float = 40.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{base_url.rstrip('/')}/api/v1/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(1.0)
    raise TimeoutError(f"API did not become healthy at {health_url} within {timeout_seconds}s")


def _seed_sample_document(base_url: str, *, api_key: str, tenant_id: str) -> str:
    """Sorgu uçlarının Qdrant'tan gerçek parça döndürebilmesi için 1 örnek belge yükler."""
    import pymupdf

    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(48, 48, 540, 740),
            (
                "CoreRAG Kurumsal El Kitabı.\n"
                "Çalışanların yıllık ücretli izin hakkı en az 20 iş günüdür.\n"
                "Haftada 2 gün uzaktan çalışma hakkı uygulanır."
            ),
        )
        pdf_bytes = bytes(doc.tobytes())

    boundary = "----CoreRAGSeedBoundary7d9f2a"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="tenant_id"\r\n\r\n{tenant_id}\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="load_seed.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + pdf_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/documents/upload",
        data=body,
        method="POST",
        headers={
            "X-API-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        upload_data = json.loads(resp.read().decode("utf-8"))

    task_id = str(upload_data["task_id"])
    document_id = str(upload_data["document_id"])
    status_url = f"{base_url.rstrip('/')}/api/v1/documents/status/{task_id}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        s_req = urllib.request.Request(status_url, headers={"X-API-Key": api_key})
        with urllib.request.urlopen(s_req, timeout=5) as s_resp:
            snap = json.loads(s_resp.read().decode("utf-8"))
        if snap.get("status") == "SUCCESS":
            return document_id
        if snap.get("status") == "FAILURE":
            raise RuntimeError(f"Seed upload task failed: {snap}")
        time.sleep(0.3)
    raise TimeoutError("Seed document task did not finish within 30s")


def _cleanup_qdrant_collection(qdrant_url: str, collection_name: str) -> None:
    req = urllib.request.Request(
        f"{qdrant_url.rstrip('/')}/collections/{collection_name}",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except urllib.error.HTTPError:
        pass


class _DockerStatsSampler:
    """Yük testi süresince arka planda periyodik ``docker stats`` örneği toplar."""

    def __init__(self, containers: Sequence[str], interval_seconds: float) -> None:
        self._containers = tuple(containers)
        self._interval = max(1.0, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[ContainerMemorySample] = []

    def capture_once(self) -> None:
        self.samples.extend(sample_docker_stats(self._containers))

    def start(self) -> None:
        self.capture_once()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.capture_once()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.capture_once()


def _build_override_compose_yaml(mock_script_path: Path) -> str:
    escaped_path = mock_script_path.resolve().as_posix()
    return f"""services:
  mock-ai:
    image: python:3.11-slim
    container_name: corerag-mock-ai
    command: ["python", "-u", "/opt/mock_ai_server.py"]
    volumes:
      - "{escaped_path}:/opt/mock_ai_server.py:ro"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request as u; u.urlopen('http://127.0.0.1:18080/health', timeout=2)",
        ]
      interval: 2s
      timeout: 2s
      retries: 10
    networks:
      - corerag-net

  api:
    environment:
      LLM_BASE_URL: http://mock-ai:18080/v1
      QDRANT_COLLECTION_NAME: {LOAD_TEST_COLLECTION}
      RATE_LIMIT_ENABLED: "false"
      LANGFUSE_ENABLED: "false"
    depends_on:
      mock-ai:
        condition: service_healthy
      redis:
        condition: service_healthy
      qdrant:
        condition: service_healthy

  celery_worker:
    environment:
      LLM_BASE_URL: http://mock-ai:18080/v1
      QDRANT_COLLECTION_NAME: {LOAD_TEST_COLLECTION}
      LANGFUSE_ENABLED: "false"
    depends_on:
      mock-ai:
        condition: service_healthy
      redis:
        condition: service_healthy
      qdrant:
        condition: service_healthy
"""


def _compose_cmd(override_path: Path | None = None) -> list[str]:
    base = [
        "docker",
        "compose",
        "--project-directory",
        str(PROJECT_ROOT),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
    ]
    if override_path is not None:
        base.extend(["-f", str(override_path)])
    return base


def _print_summary(report: dict[str, Any]) -> None:
    print("=" * 96)
    print(
        f"CoreRAG-Engine Load & Memory Report | users={report['config']['users']} "
        f"| duration={report['config']['duration']} | mock_ai={report['config']['with_mock_ai']}"
    )
    print("-" * 96)
    print(
        f"{'Method':<8} | {'Endpoint':<38} | {'Reqs':>6} | {'Fail%':>6} | "
        f"{'P50':>7} | {'P95':>7} | {'P99':>7} | {'RPS':>6}"
    )
    print("-" * 96)
    for row in report["endpoints"]:
        print(
            f"{row['method']:<8} | {row['name'][:38]:<38} | {row['request_count']:>6} | "
            f"{row['failure_rate_percent']:>5.2f}% | {row['p50_ms']:>6.1f}ms | "
            f"{row['p95_ms']:>6.1f}ms | {row['p99_ms']:>6.1f}ms | {row['rps']:>6.2f}"
        )

    print("-" * 96)
    print(
        f"{'Container':<16} | {'Start':>9} | {'Peak':>9} | {'End':>9} | "
        f"{'Delta':>9} | {'MaxCPU':>7} | {'OOM':<5} | {'Restarts':>8}"
    )
    print("-" * 96)
    for item in report["containers"]:
        print(
            f"{item['container']:<16} | {item['start_mem_mib']:>7.2f}MB | "
            f"{item['peak_mem_mib']:>7.2f}MB | {item['end_mem_mib']:>7.2f}MB | "
            f"{item['delta_mem_mib']:>+7.2f}MB | {item['max_cpu_percent']:>6.2f}% | "
            f"{str(item['oom_killed']):<5} | {item['restart_count']:>8}"
        )
    print("=" * 96)


def run_benchmark(args: argparse.Namespace) -> int:
    """Locust yük testini koşturur, bellek profiller ve JSON rapor üretir."""
    settings = get_settings()
    api_key = args.api_key or settings.API_KEY.get_secret_value()
    containers = DEFAULT_CONTAINERS
    override_path: Path | None = None
    tmp_ctx: tempfile.TemporaryDirectory[str] | None = None

    try:
        if args.with_mock_ai:
            tmp_ctx = tempfile.TemporaryDirectory(prefix="corerag-load-")
            tmp_dir = Path(tmp_ctx.name)
            mock_script = tmp_dir / "mock_ai_server.py"
            mock_script.write_text(
                build_mock_ai_server_source(settings.QDRANT_VECTOR_SIZE),
                encoding="utf-8",
            )
            override_path = tmp_dir / "docker-compose.loadtest.yml"
            override_path.write_text(_build_override_compose_yaml(mock_script), encoding="utf-8")

            subprocess.run(
                [
                    *_compose_cmd(override_path),
                    "up",
                    "-d",
                    "--force-recreate",
                    "redis",
                    "qdrant",
                    "mock-ai",
                    "api",
                    "celery_worker",
                ],
                check=True,
            )

        _wait_for_api_healthy(args.host, timeout_seconds=45.0)
        if not args.skip_seed:
            _seed_sample_document(args.host, api_key=api_key, tenant_id=args.tenant_id)

        sampler = _DockerStatsSampler(containers=containers, interval_seconds=args.sample_interval)
        sampler.start()

        with tempfile.TemporaryDirectory(prefix="corerag-locust-csv-") as csv_dir:
            csv_prefix = Path(csv_dir) / "locust"
            locust_env = dict(os.environ)
            locust_env["CORERAG_API_KEY"] = api_key
            locust_cmd = [
                sys.executable,
                "-m",
                "locust",
                "-f",
                str(PROJECT_ROOT / "locustfile.py"),
                "--headless",
                "--host",
                args.host,
                "-u",
                str(args.users),
                "-r",
                str(args.spawn_rate),
                "-t",
                args.duration,
                "--tenant-id",
                args.tenant_id,
                "--csv",
                str(csv_prefix),
                "--only-summary",
            ]
            if args.tags:
                locust_cmd.extend(["--tags", *args.tags])
            if args.accept_429:
                locust_cmd.append("--accept-429")

            locust_proc = subprocess.run(locust_cmd, env=locust_env, check=False)
            stats_csv_path = Path(f"{csv_prefix}_stats.csv")
            stats_text = (
                stats_csv_path.read_text(encoding="utf-8") if stats_csv_path.is_file() else ""
            )

        if args.cooldown_seconds > 0:
            time.sleep(args.cooldown_seconds)
        sampler.stop()

        oom_map = inspect_containers_oom_and_restarts(containers)
        endpoints = parse_locust_stats_csv(stats_text)
        container_summaries = summarize_container_memory(sampler.samples, oom_map)

        report: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "host": args.host,
            "config": {
                "users": args.users,
                "spawn_rate": args.spawn_rate,
                "duration": args.duration,
                "cooldown_seconds": args.cooldown_seconds,
                "sample_interval_seconds": args.sample_interval,
                "tenant_id": args.tenant_id,
                "with_mock_ai": args.with_mock_ai,
                "accept_429": args.accept_429,
                "tags": args.tags,
            },
            "locust_exit_code": locust_proc.returncode,
            "endpoints": [item.to_dict() for item in endpoints],
            "containers": [item.to_dict() for item in container_summaries],
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _print_summary(report)
        print(f"\nReport written to {output_path}")
        return locust_proc.returncode
    finally:
        if args.with_mock_ai:
            _cleanup_qdrant_collection(settings.QDRANT_URL, LOAD_TEST_COLLECTION)
            subprocess.run(
                ["docker", "rm", "-f", "corerag-mock-ai"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    *_compose_cmd(),
                    "up",
                    "-d",
                    "--no-start",
                    "--force-recreate",
                    "api",
                    "celery_worker",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [*_compose_cmd(), "stop", "redis", "qdrant", "api", "celery_worker"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CoreRAG-Engine Locust load test and container RAM stability runner",
    )
    parser.add_argument("--host", default="http://localhost:8000", help="Target FastAPI base URL")
    parser.add_argument(
        "--users", type=int, default=20, help="Concurrent Locust users (default: 20)"
    )
    parser.add_argument("--spawn-rate", type=int, default=5, help="User spawn rate/s (default: 5)")
    parser.add_argument("--duration", default="45s", help="Test duration (e.g. 30s, 2m)")
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=3.0,
        help="Docker stats polling interval in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=5.0,
        help="Post-load cooldown seconds before final RAM check (default: 5.0)",
    )
    parser.add_argument("--tenant-id", default="load_test_tenant", help="Isolated tenant_id")
    parser.add_argument("--api-key", default=None, help="Explicit API key (defaults to settings)")
    parser.add_argument(
        "--with-mock-ai",
        action="store_true",
        help="Spin up isolated local OpenAI mock container and temporary Qdrant load collection",
    )
    parser.add_argument(
        "--accept-429",
        action="store_true",
        help="Treat HTTP 429 rate-limit rejections as expected responses",
    )
    parser.add_argument("--tags", nargs="*", default=None, help="Optional Locust @tag filters")
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Skip initial sample document upload before Locust run",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "evals" / "load_test_report.json"),
        help="Output JSON report path",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run_benchmark(_parse_args()))
