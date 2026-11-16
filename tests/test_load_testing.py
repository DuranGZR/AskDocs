from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_load_test import build_mock_ai_server_source
from src.evals.load_metrics import (
    parse_docker_stats_line,
    parse_locust_stats_csv,
    parse_size_to_mib,
    summarize_container_memory,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_parse_size_to_mib_and_docker_stats_line() -> None:
    """Docker stats bellek birimleri ve sekme ayrılmış satırlar doğru ayrıştırılmalıdır."""
    assert parse_size_to_mib("128MiB") == 128.0
    assert parse_size_to_mib("1.5GiB") == 1536.0
    assert parse_size_to_mib("512KiB") == 0.5
    assert parse_size_to_mib("1048576B") == 1.0

    with pytest.raises(ValueError):
        parse_size_to_mib("invalid-mem")

    line = "corerag-api\t4.25%\t84.5MiB / 7.77GiB\t1.06%\t14"
    sample = parse_docker_stats_line(line)
    assert sample is not None
    assert sample.container == "corerag-api"
    assert sample.cpu_percent == 4.25
    assert sample.mem_used_mib == 84.5
    assert sample.mem_limit_mib == pytest.approx(7956.48, rel=1e-3)
    assert sample.mem_percent == 1.06
    assert sample.pids == 14
    assert parse_docker_stats_line("") is None


def test_summarize_container_memory_tracks_peak_delta_and_oom() -> None:
    """Konteyner bellek özeti başlangıç, tepe, bitiş deltasını ve OOM bilgisini hesaplamalıdır."""
    raw_lines = [
        "corerag-api\t1.0%\t80MiB / 1GiB\t7.8%\t10",
        "corerag-api\t25.0%\t112.5MiB / 1GiB\t11.0%\t14",
        "corerag-api\t2.0%\t85.0MiB / 1GiB\t8.3%\t10",
        "corerag-worker\t10.0%\t120MiB / 1GiB\t11.7%\t6",
    ]
    samples = [item for line in raw_lines if (item := parse_docker_stats_line(line)) is not None]
    summaries = summarize_container_memory(
        samples,
        oom_and_restarts={"corerag-api": (False, 0), "corerag-worker": (False, 1)},
    )

    by_name = {entry.container: entry for entry in summaries}
    api = by_name["corerag-api"]
    assert api.sample_count == 3
    assert api.start_mem_mib == 80.0
    assert api.peak_mem_mib == 112.5
    assert api.end_mem_mib == 85.0
    assert api.delta_mem_mib == 5.0
    assert api.max_cpu_percent == 25.0
    assert api.oom_killed is False
    assert api.restart_count == 0
    assert by_name["corerag-worker"].restart_count == 1


def test_parse_locust_stats_csv_computes_p50_p95_p99_and_failure_rate() -> None:
    """Locust stats CSV ayrıştırıcısı p50/p95/p99, hata oranı ve RPS metriklerini dönmelidir."""
    csv_sample = "\n".join(
        [
            "Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,"
            "Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,"
            "50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%",
            "GET,GET /api/v1/health,200,0,4,5.1,1,24,118,12.5,0.0,4,5,6,6,8,11,18,22,24,24,24",
            (
                "POST,POST /api/v1/query,100,2,28,31.4,12,115,412,6.25,0.125,"
                "28,32,35,38,44,62,89,108,115,115,115"
            ),
            ",Aggregated,300,2,10,13.9,1,115,216,18.75,0.125,10,15,22,28,36,48,74,96,115,115,115",
        ]
    )
    rows = parse_locust_stats_csv(csv_sample)
    assert len(rows) == 3

    agg = rows[-1]
    assert agg.method == "AGGREGATE"
    assert agg.name == "Aggregated"
    assert agg.request_count == 300
    assert agg.failure_count == 2
    assert agg.failure_rate_percent == pytest.approx(0.667, rel=1e-3)
    assert agg.p50_ms == 10.0
    assert agg.p95_ms == 48.0
    assert agg.p99_ms == 96.0
    assert agg.rps == 18.75


def test_build_mock_ai_server_source_compiles_cleanly() -> None:
    """Üretilen mock OpenAI sunucusu sözdizimi hatası olmadan derlenmelidir."""
    source = build_mock_ai_server_source(vector_size=3072)
    compile(source, "mock_ai_server.py", "exec")
    assert "VECTOR_SIZE = 3072" in source
    assert "data: [DONE]" in source


def test_locustfile_registers_user_tasks_in_isolated_subprocess() -> None:
    """locustfile.py gevent yan etkisini pytest sürecine taşımadan alt süreçte yüklenmelidir."""
    proc = subprocess.run(
        [sys.executable, "-m", "locust", "-f", str(PROJECT_ROOT / "locustfile.py"), "--list"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CoreRAGUser" in proc.stdout
