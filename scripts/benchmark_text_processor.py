"""5. Hafta (25. Gün) Python vs Rust (`text_processor`) saf CPU ve bellek microbenchmark aracı.

Ağ, disk I/O, Qdrant ve LLM/GPU sürelerinden tamamen izole olarak büyük metin
bloklarında (varsayılan 50 MiB) `clean_text` ve `chunk_text` performansını,
Python heap tepe bellek kullanımını (`tracemalloc`) ve çıktı eşitliğini ölçer.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import text_processor  # noqa: E402
from src.services.pdf_parser import (  # noqa: E402
    ACTIVE_TEXT_PROCESSOR_BACKEND,
    py_chunk_text,
    py_clean_text,
)

_SAMPLE_BLOCK = (
    "<script>console.log('ignored-telemetry');</script>"
    "<style>.hidden-banner { display: none; }</style>\r\n"
    "<p>AskDocs kurumsal sözleşme ve teknik mimari doküman-\ntasyonu: "
    "Her kiracı (tenant) verisi HNSW indekslerinde izole saklanır.</p>\x00\x07\r\n\r\n"
    "Madde 14.2:   Çalışanların gizlilik   politikası ve şif-\nreli erişim "
    "kuralları (ğüşıöç ĞÜŞİÖÇ 🚀) tüm PDF sayfalarında korunur.\t\t"
    "Sistem, yüksek hacimli belgeleri deterministik UUID5 kimlikleriyle "
    "idempotent biçimde işler.  Güvenilirlik için her parça cümle veya "
    "kelime sınırından hizalanır.\n\n\n"
    "Section 4.1: Retrieval precision under overlapping technical papers "
    "requires clean hyphenation repair, HTML tag stripping, and predictable "
    "sentence-aligned windowing at production scale.\n\n"
)


@dataclass(frozen=True, slots=True)
class StageBenchmarkMetrics:
    """Tek bir işlem aşaması (clean_text veya chunk_text) için Python vs Rust ölçüm özeti."""

    stage: str
    python_median_ms: float
    python_mean_ms: float
    python_min_ms: float
    python_throughput_mib_per_sec: float
    python_heap_peak_mib: float
    rust_median_ms: float
    rust_mean_ms: float
    rust_min_ms: float
    rust_throughput_mib_per_sec: float
    rust_heap_peak_mib: float
    speedup: float
    parity_verified: bool
    output_count: int


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Tam benchmark koşusunun makine ve insan tarafından okunabilir raporu."""

    corpus_size_bytes: int
    corpus_size_mib: float
    corpus_chars: int
    chunk_size: int
    chunk_overlap: int
    iterations: int
    active_backend: str
    rust_version: str
    clean_text: StageBenchmarkMetrics
    chunk_text: StageBenchmarkMetrics


def generate_benchmark_corpus(target_size_mb: float) -> str:
    """HTML, kontrol karakteri, tire bölünmesi ve Türkçe metin içeren korpus üretir."""
    if target_size_mb <= 0:
        raise ValueError("target_size_mb must be positive")

    target_bytes = max(1, int(target_size_mb * 1024 * 1024))
    block_bytes = len(_SAMPLE_BLOCK.encode("utf-8"))
    repeat_count = max(1, (target_bytes + block_bytes - 1) // block_bytes)
    corpus = _SAMPLE_BLOCK * repeat_count

    encoded = corpus.encode("utf-8")
    if len(encoded) > target_bytes:
        return encoded[:target_bytes].decode("utf-8", errors="ignore")
    return corpus


_T = TypeVar("_T")


def _measure_peak_heap_and_result(func: Callable[[], _T]) -> tuple[float, _T]:
    """Zamanlama döngüsünü kirletmeden izole `tracemalloc` zirve heap kullanımını ölçer."""
    gc.collect()
    tracemalloc.start()
    try:
        result = func()
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak_bytes / (1024.0 * 1024.0), result


def _time_function(func: Callable[[], _T], *, iterations: int) -> list[float]:
    """Isınma (warmup) sonrasında milisaniye cinsinden saf yürütme sürelerini toplar."""
    _ = func()
    timings_ms: list[float] = []
    for _ in range(iterations):
        gc.collect()
        t_start = time.perf_counter()
        _ = func()
        timings_ms.append((time.perf_counter() - t_start) * 1000.0)
    return timings_ms


def _benchmark_stage(
    *,
    stage: str,
    corpus_mib: float,
    iterations: int,
    py_func: Callable[[], _T],
    rust_func: Callable[[], _T],
    count_fn: Callable[[_T], int],
) -> StageBenchmarkMetrics:
    py_heap_mib, py_result = _measure_peak_heap_and_result(py_func)
    rust_heap_mib, rust_result = _measure_peak_heap_and_result(rust_func)
    parity_verified = py_result == rust_result
    output_count = count_fn(rust_result)
    del py_result, rust_result
    gc.collect()

    rust_timings = _time_function(rust_func, iterations=iterations)
    py_timings = _time_function(py_func, iterations=iterations)

    py_median = statistics.median(py_timings)
    rust_median = statistics.median(rust_timings)

    return StageBenchmarkMetrics(
        stage=stage,
        python_median_ms=round(py_median, 3),
        python_mean_ms=round(statistics.fmean(py_timings), 3),
        python_min_ms=round(min(py_timings), 3),
        python_throughput_mib_per_sec=round(corpus_mib / (py_median / 1000.0), 2),
        python_heap_peak_mib=round(py_heap_mib, 3),
        rust_median_ms=round(rust_median, 3),
        rust_mean_ms=round(statistics.fmean(rust_timings), 3),
        rust_min_ms=round(min(rust_timings), 3),
        rust_throughput_mib_per_sec=round(corpus_mib / (rust_median / 1000.0), 2),
        rust_heap_peak_mib=round(rust_heap_mib, 3),
        speedup=round(py_median / rust_median, 2) if rust_median > 0 else 0.0,
        parity_verified=parity_verified,
        output_count=output_count,
    )


def run_benchmark(
    *,
    target_size_mb: float = 50.0,
    chunk_size: int = 1200,
    chunk_overlap: int = 150,
    iterations: int = 3,
) -> BenchmarkReport:
    """Python ve Rust metin işleme motorlarını hız, heap belleği ve eşitlik açısından kıyaslar."""
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")

    corpus = generate_benchmark_corpus(target_size_mb)
    corpus_bytes = len(corpus.encode("utf-8"))
    corpus_mib = corpus_bytes / (1024.0 * 1024.0)

    clean_metrics = _benchmark_stage(
        stage="clean_text",
        corpus_mib=corpus_mib,
        iterations=iterations,
        py_func=lambda: py_clean_text(corpus),
        rust_func=lambda: text_processor.clean_text(corpus),
        count_fn=len,
    )
    chunk_metrics = _benchmark_stage(
        stage="chunk_text",
        corpus_mib=corpus_mib,
        iterations=iterations,
        py_func=lambda: py_chunk_text(corpus, chunk_size=chunk_size, chunk_overlap=chunk_overlap),
        rust_func=lambda: text_processor.chunk_text(
            corpus,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        ),
        count_fn=len,
    )

    return BenchmarkReport(
        corpus_size_bytes=corpus_bytes,
        corpus_size_mib=round(corpus_mib, 2),
        corpus_chars=len(corpus),
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        iterations=iterations,
        active_backend=ACTIVE_TEXT_PROCESSOR_BACKEND,
        rust_version=text_processor.version(),
        clean_text=clean_metrics,
        chunk_text=chunk_metrics,
    )


def _print_report(report: BenchmarkReport) -> None:
    print("=" * 88)
    print(
        f"CoreRAG Text Processor Benchmark | {report.corpus_size_mib:.2f} MiB "
        f"({report.corpus_chars:,} chars) | chunk={report.chunk_size}/{report.chunk_overlap} "
        f"| backend={report.active_backend} (v{report.rust_version}) | runs={report.iterations}"
    )
    print("-" * 88)
    print(
        f"{'Stage':<12} | {'Py Median':>11} | {'Rust Median':>11} | {'Speedup':>8} | "
        f"{'Py MiB/s':>9} | {'Rust MiB/s':>10} | {'Py Heap':>9} | {'Rust Heap':>9} | {'Parity':>6}"
    )
    print("-" * 88)
    for row in (report.clean_text, report.chunk_text):
        print(
            f"{row.stage:<12} | {row.python_median_ms:8.2f} ms | {row.rust_median_ms:8.2f} ms | "
            f"{row.speedup:6.2f}x | {row.python_throughput_mib_per_sec:9.2f} | "
            f"{row.rust_throughput_mib_per_sec:10.2f} | {row.python_heap_peak_mib:6.2f} MiB | "
            f"{row.rust_heap_peak_mib:6.2f} MiB | {str(row.parity_verified):>6}"
        )
    print("=" * 88)
    print(f"Produced chunks (chunk_text): {report.chunk_text.output_count:,}")


def build_parser() -> argparse.ArgumentParser:
    """CLI argüman ayrıştırıcısını oluşturur."""
    parser = argparse.ArgumentParser(
        description="Benchmark pure CPU Python vs Rust text cleaning and chunking."
    )
    parser.add_argument(
        "--size-mb",
        type=float,
        default=50.0,
        help="Target corpus size in MiB (default: 50.0).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1200,
        help="Target chunk size in characters (default: 1200).",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=150,
        help="Overlap between chunks in characters (default: 150).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Measured iterations per stage after warmup (default: 3).",
    )
    parser.add_argument(
        "--output",
        default="evals/text_processor_benchmark.json",
        help="Path to write JSON benchmark report.",
    )
    return parser


def main() -> int:
    """Benchmark CLI giriş noktası."""
    args = build_parser().parse_args()
    report = run_benchmark(
        target_size_mb=args.size_mb,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        iterations=args.iterations,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    _print_report(report)
    print(f"Saved benchmark JSON report to: {output_path}")
    return 0 if (report.clean_text.parity_verified and report.chunk_text.parity_verified) else 1


if __name__ == "__main__":
    raise SystemExit(main())
