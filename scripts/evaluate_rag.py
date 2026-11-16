"""Hafta 4 (17. Gün) Altın Veri Seti üzerinde RAG Kalite Değerlendirme (Eval) script'i.

Mevcut Qdrant + LLM hattını kullanarak Recall@K, MRR, Faithfulness, Keyword Coverage
ve Negative Abstention skorlarını hesaplar; aday `score_threshold` değerlerini kıyaslar
ve sonuçları Langfuse'a kaydeder.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import get_settings  # noqa: E402
from src.core.observability import configure_observability, shutdown_observability  # noqa: E402
from src.evals.rag_eval import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    EvalSummaryReport,
    evaluate_rag_dataset,
    load_golden_dataset,
)
from src.services.llm_service import AsyncOpenAICompatibleLLMClient  # noqa: E402
from src.services.qdrant_service import AsyncQdrantService  # noqa: E402


def _parse_thresholds(raw: str | None) -> tuple[float, ...]:
    if not raw or not raw.strip():
        return DEFAULT_THRESHOLDS
    return tuple(float(part.strip()) for part in raw.split(",") if part.strip())


def _print_report(report: EvalSummaryReport) -> None:
    print("=" * 84)
    print(f"CoreRAG RAG Quality Evaluation  | run_id={report.eval_run_id}")
    print(
        f"Model={report.llm_model} | Embedding={report.embedding_model} "
        f"| top_k={report.top_k} | threshold={report.score_threshold} "
        f"| judge={report.judge_mode}"
    )
    print("-" * 84)
    print(
        f"Questions        : {report.total_questions} "
        f"(answerable={report.answerable_questions}, unanswerable={report.unanswerable_questions})"
    )
    print(f"Mean Recall@{report.top_k:<2}    : {report.mean_recall_at_k:.4f}")
    print(f"Mean MRR         : {report.mean_mrr:.4f}")
    print(f"Mean Faithfulness: {report.mean_faithfulness:.4f}")
    print(f"Keyword Coverage : {report.mean_keyword_coverage:.4f}")
    print(f"Abstention Acc.  : {report.abstention_accuracy:.4f}")
    if report.recommended_threshold is not None:
        print(f"Recommended Thr. : {report.recommended_threshold:.2f}")

    if report.threshold_sweep:
        print("\n--- Threshold Sweep ---")
        print(
            f"{'Threshold':>9} | {'Recall@K':>8} | {'MRR':>8} | {'AvgChunks':>9} | {'NegNoise':>8}"
        )
        print("-" * 54)
        for row in report.threshold_sweep:
            print(
                f"{row.score_threshold:9.2f} | {row.recall_at_k:8.4f} | {row.mrr:8.4f} | "
                f"{row.avg_retrieved_chunks:9.2f} | {row.negative_noise_rate:8.4f}"
            )

    print("\n--- Per-Question Breakdown ---")
    print(
        f"{'ID':<4} | {'Ans':<3} | {'R@K':>4} | {'MRR':>6} | "
        f"{'Faith':>5} | {'Cov':>5} | {'TopSc':>6} | {'Pages'}"
    )
    print("-" * 84)
    for item in report.results:
        r_str = f"{item.recall_at_k:.1f}" if item.recall_at_k is not None else " n/a"
        m_str = f"{item.mrr:.3f}" if item.mrr is not None else "  n/a "
        c_str = f"{item.keyword_coverage:.2f}" if item.keyword_coverage is not None else " n/a "
        t_str = f"{item.top_score:.4f}" if item.top_score is not None else "  none"
        print(
            f"{item.id:<4} | {'Y' if item.answerable else 'N':<3} | {r_str:>4} | {m_str:>6} | "
            f"{item.faithfulness:5.2f} | {c_str:>5} | {t_str:>6} | {item.retrieved_pages}"
        )
        if item.reasons:
            print(f"     notes: {'; '.join(item.reasons)}")
    print("=" * 84)


async def _run_cli(args: argparse.Namespace) -> int:
    settings = get_settings()
    dataset = load_golden_dataset(Path(args.dataset))
    top_k = args.top_k if args.top_k is not None else settings.RAG_TOP_K
    score_threshold = (
        args.score_threshold if args.score_threshold is not None else settings.RAG_SCORE_THRESHOLD
    )
    thresholds = _parse_thresholds(args.thresholds)

    langfuse_client = None if args.no_langfuse else configure_observability(settings)
    llm_client = AsyncOpenAICompatibleLLMClient(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        llm_model=settings.LLM_MODEL,
        embedding_model=settings.EMBEDDING_MODEL,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
    )
    vector_db = AsyncQdrantService(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
        vector_size=settings.QDRANT_VECTOR_SIZE,
    )

    try:
        report = await evaluate_rag_dataset(
            dataset=dataset,
            llm_client=llm_client,
            vector_db=vector_db,
            llm_model=settings.LLM_MODEL,
            embedding_model=settings.EMBEDDING_MODEL,
            top_k=top_k,
            score_threshold=score_threshold,
            use_llm_judge=args.llm_judge,
            sweep_thresholds=thresholds,
            delay_seconds=args.delay_seconds,
            embedding_batch_size=settings.EMBEDDING_BATCH_SIZE,
            langfuse_client=langfuse_client,
        )
    finally:
        await llm_client.close()
        await vector_db.close()
        if langfuse_client is not None:
            shutdown_observability()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _print_report(report)
    print(f"Saved JSON report to: {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI argüman ayrıştırıcısını oluşturur."""
    parser = argparse.ArgumentParser(description="Evaluate CoreRAG retrieval and answer quality.")
    parser.add_argument(
        "--dataset",
        default="evals/golden_dataset.json",
        help="Path to the golden dataset JSON file.",
    )
    parser.add_argument(
        "--output",
        default="evals/latest_eval_report.json",
        help="Path to write the JSON evaluation report.",
    )
    parser.add_argument("--top-k", type=int, default=None, help="Override RAG_TOP_K.")
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=None,
        help="Override RAG_SCORE_THRESHOLD.",
    )
    parser.add_argument(
        "--thresholds",
        default="0.30,0.40,0.45,0.50,0.60,0.70",
        help="Comma-separated score_threshold candidates for sweep.",
    )
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Use LLM-as-a-judge for faithfulness scoring (falls back cleanly on errors).",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=4.2,
        help="Delay between LLM requests to respect rate limits.",
    )
    parser.add_argument(
        "--no-langfuse",
        action="store_true",
        help="Disable Langfuse score reporting for this run.",
    )
    return parser


def main() -> int:
    """Değerlendirme CLI giriş noktası."""
    args = build_parser().parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
