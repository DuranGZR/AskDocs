"""Yük testi gecikme (p50/p95/p99) ve Docker konteyner bellek kararlılığı analiz modülü.

Saf Python ile yazılmıştır; ``gevent``/``locust`` içe aktarmadığı için
``pytest`` asenkron test ortamını etkilemeden birim testlerle doğrulanabilir.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

_MEM_PART_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)\s*$")

_SIZE_TO_MIB: dict[str, float] = {
    "B": 1.0 / (1024.0 * 1024.0),
    "KB": 1000.0 / (1024.0 * 1024.0),
    "KIB": 1.0 / 1024.0,
    "MB": (1000.0 * 1000.0) / (1024.0 * 1024.0),
    "MIB": 1.0,
    "GB": (1000.0 * 1000.0 * 1000.0) / (1024.0 * 1024.0),
    "GIB": 1024.0,
}


@dataclass(frozen=True, slots=True)
class ContainerMemorySample:
    """Tek bir ``docker stats --no-stream`` örneğinden ayrıştırılan kaynak ölçümü."""

    container: str
    cpu_percent: float
    mem_used_mib: float
    mem_limit_mib: float
    mem_percent: float
    pids: int


@dataclass(frozen=True, slots=True)
class ContainerMemorySummary:
    """Yük testi boyunca bir konteynerin RAM/CPU ve OOM özet raporu."""

    container: str
    sample_count: int
    start_mem_mib: float
    peak_mem_mib: float
    end_mem_mib: float
    delta_mem_mib: float
    mem_limit_mib: float
    max_mem_percent: float
    avg_cpu_percent: float
    max_cpu_percent: float
    max_pids: int
    oom_killed: bool
    restart_count: int

    def to_dict(self) -> dict[str, object]:
        """Rapor serileştirmesi için sözlük temsiline dönüştürür."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EndpointLoadMetric:
    """Locust CSV çıktısından ayrıştırılan uç nokta gecikme ve hata metriği."""

    method: str
    name: str
    request_count: int
    failure_count: int
    failure_rate_percent: float
    avg_ms: float
    min_ms: float
    max_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    rps: float

    def to_dict(self) -> dict[str, object]:
        """Rapor serileştirmesi için sözlük temsiline dönüştürür."""
        return asdict(self)


def parse_size_to_mib(raw_size: str) -> float:
    """Docker stats bellek değerini (örn. ``142.5MiB``, ``1.2GiB``) MiB cinsine çevirir."""
    match = _MEM_PART_RE.fullmatch(raw_size.strip())
    if match is None:
        raise ValueError(f"Unrecognized memory size format: {raw_size!r}")
    value = float(match.group(1))
    unit = match.group(2).upper()
    factor = _SIZE_TO_MIB.get(unit)
    if factor is None:
        raise ValueError(f"Unsupported memory unit: {unit!r}")
    return round(value * factor, 3)


def parse_docker_stats_line(line: str) -> ContainerMemorySample | None:
    """Tab ile ayrılmış tek bir ``docker stats`` satırını ayrıştırır."""
    stripped = line.strip()
    if not stripped:
        return None
    parts = [part.strip() for part in stripped.split("\t")]
    if len(parts) < 5:
        return None

    name, raw_cpu, raw_mem, raw_mem_pct, raw_pids = parts[:5]
    if "/" not in raw_mem:
        return None
    used_str, limit_str = (piece.strip() for piece in raw_mem.split("/", maxsplit=1))

    cpu_pct = float(raw_cpu.rstrip("%").strip() or "0")
    mem_pct = float(raw_mem_pct.rstrip("%").strip() or "0")
    pids = int(raw_pids or "0")
    return ContainerMemorySample(
        container=name,
        cpu_percent=round(cpu_pct, 2),
        mem_used_mib=parse_size_to_mib(used_str),
        mem_limit_mib=parse_size_to_mib(limit_str),
        mem_percent=round(mem_pct, 2),
        pids=pids,
    )


def summarize_container_memory(
    samples: Sequence[ContainerMemorySample],
    oom_and_restarts: Mapping[str, tuple[bool, int]] | None = None,
) -> list[ContainerMemorySummary]:
    """Konteyner bazlı RAM başlangıç, tepe, bitiş, delta ve OOM özetini hesaplar."""
    grouped: dict[str, list[ContainerMemorySample]] = {}
    for sample in samples:
        grouped.setdefault(sample.container, []).append(sample)

    summaries: list[ContainerMemorySummary] = []
    status_lookup = oom_and_restarts or {}
    for container in sorted(grouped):
        items = grouped[container]
        start_mib = items[0].mem_used_mib
        end_mib = items[-1].mem_used_mib
        peak_mib = max(item.mem_used_mib for item in items)
        limit_mib = max(item.mem_limit_mib for item in items)
        max_mem_pct = max(item.mem_percent for item in items)
        avg_cpu = sum(item.cpu_percent for item in items) / len(items)
        max_cpu = max(item.cpu_percent for item in items)
        max_pids = max(item.pids for item in items)
        oom_killed, restarts = status_lookup.get(container, (False, 0))

        summaries.append(
            ContainerMemorySummary(
                container=container,
                sample_count=len(items),
                start_mem_mib=round(start_mib, 2),
                peak_mem_mib=round(peak_mib, 2),
                end_mem_mib=round(end_mib, 2),
                delta_mem_mib=round(end_mib - start_mib, 2),
                mem_limit_mib=round(limit_mib, 2),
                max_mem_percent=round(max_mem_pct, 2),
                avg_cpu_percent=round(avg_cpu, 2),
                max_cpu_percent=round(max_cpu, 2),
                max_pids=max_pids,
                oom_killed=oom_killed,
                restart_count=restarts,
            )
        )
    return summaries


def parse_locust_stats_csv(csv_text: str) -> list[EndpointLoadMetric]:
    """Locust ``*_stats.csv`` çıktısını uç nokta gecikme ve RPS metriklerine dönüştürür."""
    reader = csv.DictReader(io.StringIO(csv_text))
    metrics: list[EndpointLoadMetric] = []
    for row in reader:
        name = (row.get("Name") or "").strip()
        method = (row.get("Type") or "").strip() or "AGGREGATE"
        if not name:
            continue
        req_count = int(float(row.get("Request Count") or "0"))
        fail_count = int(float(row.get("Failure Count") or "0"))
        fail_rate = (fail_count / req_count * 100.0) if req_count > 0 else 0.0
        metrics.append(
            EndpointLoadMetric(
                method=method,
                name=name,
                request_count=req_count,
                failure_count=fail_count,
                failure_rate_percent=round(fail_rate, 3),
                avg_ms=round(float(row.get("Average Response Time") or "0"), 2),
                min_ms=round(float(row.get("Min Response Time") or "0"), 2),
                max_ms=round(float(row.get("Max Response Time") or "0"), 2),
                p50_ms=round(float(row.get("Median Response Time") or row.get("50%") or "0"), 2),
                p95_ms=round(float(row.get("95%") or "0"), 2),
                p99_ms=round(float(row.get("99%") or "0"), 2),
                rps=round(float(row.get("Requests/s") or "0"), 2),
            )
        )
    return metrics
