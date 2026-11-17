import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from src.api.v1.query import build_rag_messages, format_context_block
from src.core.observability import get_langfuse_client, utc_now
from src.services.base import BaseLLMClient, BaseVectorDB, ChatMessage, ScoredChunk

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS: tuple[float, ...] = (0.30, 0.40, 0.45, 0.50, 0.60, 0.70)
FAITHFULNESS_CITATION_WEIGHT = 0.4
FAITHFULNESS_GROUNDING_WEIGHT = 0.6

_SOURCE_BLOCK_RE = re.compile(r"\[([^\[\]]*source[^\[\]]*)\]", flags=re.IGNORECASE)
_INT_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", flags=re.IGNORECASE)

_ABSTENTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:bilgi|veri|detay|rakam)\w*(?:\s+\S+){0,4}\s+"
        r"(?:bulunmamaktadır|bulunmuyor|yer\s+almamaktadır|mevcut\s+değil)"
    ),
    re.compile(r"\byeterli\s+bilgi(?:ye)?\s+(?:yok|sahip\s+değil|bulunmamaktadır)"),
    re.compile(r"\bbelgelerde\b.*\bbulunmamaktadır\b"),
    re.compile(r"\bbilmiyorum\b"),
    re.compile(r"\b(?:do|does)\s+not\s+(?:contain|provide|mention)\b"),
    re.compile(r"\b(?:don't|do\s+not)\s+know\b"),
    re.compile(r"\b(?:not\s+enough|insufficient|no)\s+information\b"),
    re.compile(r"\bcannot\s+(?:be\s+)?answer(?:ed)?\s+based\s+on\b"),
    re.compile(r"\bnot\s+mentioned\s+in\s+the\s+provided\b"),
    re.compile(r"\b(?:is|are)\s+not\s+(?:mentioned|stated|specified|reported|included)\b"),
)

_LLM_JUDGE_SYSTEM_PROMPT = (
    "You are an impartial RAG faithfulness evaluator. Given <context>, <question>, and "
    "<answer>, evaluate whether the answer is strictly grounded ONLY in <context>.\n"
    "Rules:\n"
    "- If answerable=false, an answer that clearly declines / states missing info scores 1.0, "
    "while any fabricated specific fact scores 0.0.\n"
    "- If answerable=true, an answer supported by <context> with no fabricated facts scores "
    "near 1.0; unsupported claims or wrong false refusals lower the score.\n"
    'Respond ONLY with raw JSON: {"score": <float 0.0..1.0>, "reason": "<short reason>"}'
)


class GoldenEvalItem(BaseModel):
    """Altın değerlendirme veri setindeki tek bir test sorusunu tanımlar."""

    id: str = Field(..., min_length=1, max_length=64)
    category: str = Field(default="general", min_length=1, max_length=64)
    question: str = Field(..., min_length=3, max_length=2000)
    answerable: bool = True
    source_file: str | None = None
    expected_document_id: str | None = None
    expected_pages: list[int] = Field(default_factory=list)
    reference_answer: str = Field(..., min_length=1, max_length=4000)
    expected_keywords: list[str] = Field(default_factory=list)


class GoldenDataset(BaseModel):
    """RAG kalite ölçümünde kullanılan altın soru-cevap veri seti şeması."""

    version: str = Field(default="1.0", min_length=1, max_length=32)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    documents: dict[str, str] = Field(default_factory=dict)
    items: list[GoldenEvalItem] = Field(..., min_length=1)

    @model_validator(mode="after")
    def resolve_and_validate_items(self) -> Self:
        """Kaynak dosya eşlemesinden eksik document_id alanlarını doldurur ve doğrular."""
        seen_ids: set[str] = set()
        for item in self.items:
            if item.id in seen_ids:
                raise ValueError(f"Duplicate golden dataset item id: {item.id}")
            seen_ids.add(item.id)

            if item.expected_document_id is None and item.source_file:
                item.expected_document_id = self.documents.get(item.source_file)

            if item.answerable and not item.expected_document_id:
                raise ValueError(
                    f"Answerable item '{item.id}' must define expected_document_id "
                    "or a source_file mapped in dataset.documents"
                )
        return self


class ThresholdSweepItem(BaseModel):
    """Tek bir `score_threshold` adayı için retrieval başarım özetini tutar."""

    score_threshold: float
    top_k: int
    answerable_queries: int
    unanswerable_queries: int
    recall_at_k: float
    mrr: float
    avg_retrieved_chunks: float
    negative_noise_rate: float


class QuestionEvalResult(BaseModel):
    """Tek bir soru için retrieval, generation ve groundedness değerlendirme sonucudur."""

    id: str
    category: str
    question: str
    answerable: bool
    expected_document_id: str | None
    expected_pages: list[int]
    retrieved_count: int
    retrieved_document_ids: list[str]
    retrieved_pages: list[int]
    top_score: float | None
    recall_at_k: float | None
    mrr: float | None
    cited_indices: list[int]
    keyword_coverage: float | None
    abstained: bool
    faithfulness: float
    faithfulness_source: str
    reasons: list[str] = Field(default_factory=list)
    answer: str
    latency_ms: float


class EvalSummaryReport(BaseModel):
    """Tüm veri seti koşusunun toplu doğruluk, retrieval ve eşik analiz raporu."""

    eval_run_id: str
    dataset_version: str
    tenant_id: str
    llm_model: str
    embedding_model: str
    top_k: int
    score_threshold: float
    judge_mode: str
    total_questions: int
    answerable_questions: int
    unanswerable_questions: int
    mean_recall_at_k: float
    mean_mrr: float
    mean_faithfulness: float
    mean_keyword_coverage: float
    abstention_accuracy: float
    recommended_threshold: float | None = None
    threshold_sweep: list[ThresholdSweepItem] = Field(default_factory=list)
    results: list[QuestionEvalResult] = Field(default_factory=list)
    evaluated_at: str


def _normalize_text(text: str) -> str:
    """Türkçe büyük İ birleşim noktasını, PDF ligatürlerini ve boşlukları normalize eder."""
    normalized = unicodedata.normalize("NFKC", text).lower().replace("\u0307", "")
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def load_golden_dataset(path: Path) -> GoldenDataset:
    """JSON dosyasından altın değerlendirme veri setini okuyup doğrular."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return GoldenDataset.model_validate(raw)


def extract_citation_indices(answer: str) -> list[int]:
    """Yanıt metnindeki `[Source 1]` / `[Source 1, Source 3]` atıf numaralarını çıkarır."""
    indices: list[int] = []
    seen: set[int] = set()
    for block in _SOURCE_BLOCK_RE.findall(answer):
        for match in _INT_RE.findall(block):
            idx = int(match)
            if idx not in seen:
                seen.add(idx)
                indices.append(idx)
    return indices


def is_abstention_answer(answer: str) -> bool:
    """Yanıtın belgelerde yeterli bilgi bulunmadığını açıkça belirtip belirtmediğini döner."""
    norm = _normalize_text(answer)
    if not norm:
        return False
    return any(pattern.search(norm) for pattern in _ABSTENTION_PATTERNS)


def compute_keyword_coverage(answer: str, expected_keywords: Sequence[str]) -> float:
    """Referans anahtar kavramların (`alt1|alt2` destekli) yanıtta kapsanma oranını hesaplar."""
    if not expected_keywords:
        return 1.0
    norm_answer = _normalize_text(answer)
    matched = 0
    for spec in expected_keywords:
        alternatives = [_normalize_text(part) for part in spec.split("|") if _normalize_text(part)]
        if any(alt in norm_answer for alt in alternatives):
            matched += 1
    return round(matched / len(expected_keywords), 4)


def _is_relevant_chunk(
    scored: ScoredChunk,
    expected_document_id: str,
    expected_pages: Sequence[int],
) -> bool:
    """Getirilen parçanın beklenen doküman ve sayfa ölçütüne uyup uymadığını kontrol eder."""
    chunk = scored.chunk
    if chunk.document_id != expected_document_id:
        return False
    if expected_pages and chunk.page_number not in expected_pages:
        return False
    return True


def compute_retrieval_metrics(
    *,
    scored_chunks: Sequence[ScoredChunk],
    expected_document_id: str | None,
    expected_pages: Sequence[int],
    top_k: int,
) -> tuple[float | None, float | None]:
    """Tek soru için Recall@K ve Reciprocal Rank (MRR bileşeni) değerlerini hesaplar."""
    if expected_document_id is None:
        return None, None

    window = list(scored_chunks[: max(1, top_k)])
    for rank, scored in enumerate(window, start=1):
        if _is_relevant_chunk(scored, expected_document_id, expected_pages):
            return 1.0, round(1.0 / rank, 4)
    return 0.0, 0.0


def compute_citation_faithfulness(
    *,
    answer: str,
    answerable: bool,
    retrieved_chunks: Sequence[ScoredChunk],
    expected_document_id: str | None,
    expected_pages: Sequence[int],
    expected_keywords: Sequence[str],
) -> tuple[float, list[str]]:
    """Atıf geçerliliği, reddetme doğruluğu ve referans kapsama ile faithfulness puanlar."""
    if not answer.strip():
        return 0.0, ["Empty generated answer"]

    abstained = is_abstention_answer(answer)
    if not answerable:
        if abstained:
            return 1.0, ["Correctly abstained on unanswerable query"]
        return 0.0, ["Hallucinated an answer on unanswerable query"]

    if abstained:
        return 0.0, ["False refusal on answerable query"]

    reasons: list[str] = []
    cited_indices = extract_citation_indices(answer)
    citation_score = 0.0

    if not cited_indices:
        if retrieved_chunks:
            reasons.append("Missing [Source N] citation in generated answer")
        else:
            reasons.append("No retrieved context chunks available")
    elif any(idx < 1 or idx > len(retrieved_chunks) for idx in cited_indices):
        reasons.append("Contains out-of-range phantom [Source N] citation")
    else:
        cited_chunks = [retrieved_chunks[idx - 1] for idx in cited_indices]
        if expected_document_id is None:
            citation_score = 1.0
        elif any(
            _is_relevant_chunk(item, expected_document_id, expected_pages) for item in cited_chunks
        ):
            citation_score = 1.0
        elif any(item.chunk.document_id == expected_document_id for item in cited_chunks):
            citation_score = 0.5
            reasons.append("Cited expected document on a non-target page")
        else:
            reasons.append("Cited chunks do not match expected source document")

    coverage = compute_keyword_coverage(answer, expected_keywords)
    if coverage < 1.0 and expected_keywords:
        reasons.append(f"Keyword coverage={coverage:.2f}")

    score = (FAITHFULNESS_CITATION_WEIGHT * citation_score) + (
        FAITHFULNESS_GROUNDING_WEIGHT * coverage
    )
    return round(score, 4), reasons


async def judge_faithfulness_with_llm(
    *,
    llm_client: BaseLLMClient,
    question: str,
    answer: str,
    answerable: bool,
    retrieved_chunks: Sequence[ScoredChunk],
    fallback_score: float,
    fallback_reasons: Sequence[str],
) -> tuple[float, list[str], str]:
    """İsteğe bağlı LLM-as-a-judge ile yanıtın bağlama sadakatini 0.0-1.0 arası puanlar."""
    context_block = format_context_block(retrieved_chunks)
    judge_user_prompt = (
        f"{context_block}\n\n"
        f"<question answerable={str(answerable).lower()}>{question}</question>\n\n"
        f"<answer>{answer}</answer>"
    )
    messages = [
        ChatMessage(role="system", content=_LLM_JUDGE_SYSTEM_PROMPT),
        ChatMessage(role="user", content=judge_user_prompt),
    ]
    try:
        raw = await llm_client.generate(messages=messages, temperature=0.0, max_tokens=200)
        cleaned = _JSON_FENCE_RE.sub("", raw.strip()).strip()
        parsed = json.loads(cleaned)
        raw_score = float(parsed["score"])
        clamped = round(max(0.0, min(1.0, raw_score)), 4)
        reason = str(parsed.get("reason", "LLM judge evaluation")).strip()
        return clamped, ([reason] if reason else []), "llm_judge"
    except Exception as exc:
        logger.warning("LLM judge failed (fallback to citation faithfulness): %s", exc)
        return (
            fallback_score,
            [*fallback_reasons, f"LLM judge fallback: {type(exc).__name__}"],
            ("citation_fallback"),
        )


def _mean_or_zero(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _pick_recommended_threshold(sweep: Sequence[ThresholdSweepItem]) -> float | None:
    """En yüksek Recall@K ve MRR'ı en az negatif gürültüyle sağlayan eşiği seçer."""
    if not sweep:
        return None
    ranked = sorted(
        sweep,
        key=lambda item: (
            item.recall_at_k,
            item.mrr,
            -item.negative_noise_rate,
            item.score_threshold,
        ),
        reverse=True,
    )
    return ranked[0].score_threshold


async def run_threshold_sweep(
    *,
    dataset: GoldenDataset,
    query_vectors: Sequence[Sequence[float]],
    vector_db: BaseVectorDB,
    top_k: int,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
) -> tuple[list[ThresholdSweepItem], float | None]:
    """Aynı sorgu vektörlerini yeniden kullanarak aday `score_threshold` değerlerini kıyaslar."""
    if len(query_vectors) != len(dataset.items):
        raise ValueError("query_vectors length must match dataset.items length")

    sweep_items: list[ThresholdSweepItem] = []
    for threshold in thresholds:
        recalls: list[float] = []
        mrrs: list[float] = []
        chunk_counts: list[float] = []
        noisy_negatives = 0
        unanswerable_total = 0

        for item, vector in zip(dataset.items, query_vectors, strict=True):
            chunks = await vector_db.search(
                query_vector=vector,
                tenant_id=dataset.tenant_id,
                top_k=top_k,
                score_threshold=threshold,
            )
            if item.answerable:
                chunk_counts.append(float(len(chunks)))
                recall, mrr = compute_retrieval_metrics(
                    scored_chunks=chunks,
                    expected_document_id=item.expected_document_id,
                    expected_pages=item.expected_pages,
                    top_k=top_k,
                )
                if recall is not None and mrr is not None:
                    recalls.append(recall)
                    mrrs.append(mrr)
            else:
                unanswerable_total += 1
                if chunks:
                    noisy_negatives += 1

        noise_rate = (
            round(noisy_negatives / unanswerable_total, 4) if unanswerable_total > 0 else 0.0
        )
        sweep_items.append(
            ThresholdSweepItem(
                score_threshold=round(threshold, 2),
                top_k=top_k,
                answerable_queries=len(recalls),
                unanswerable_queries=unanswerable_total,
                recall_at_k=_mean_or_zero(recalls),
                mrr=_mean_or_zero(mrrs),
                avg_retrieved_chunks=round(_mean_or_zero(chunk_counts), 2),
                negative_noise_rate=noise_rate,
            )
        )

    return sweep_items, _pick_recommended_threshold(sweep_items)


def record_eval_scores_to_langfuse(
    *,
    eval_run_id: str,
    tenant_id: str,
    result: QuestionEvalResult,
    score_threshold: float,
    client: Any = None,
) -> None:
    """Soru bazlı retrieval/faithfulness skorlarını Langfuse trace ve Score API'sine yazar."""
    resolved_client = client if client is not None else get_langfuse_client()
    if resolved_client is None:
        return

    try:
        trace = resolved_client.trace(
            name="eval.rag_question",
            user_id=tenant_id,
            session_id=eval_run_id,
            input={
                "question_id": result.id,
                "category": result.category,
                "question": result.question,
                "answerable": result.answerable,
                "score_threshold": score_threshold,
            },
            output={
                "answer": result.answer,
                "abstained": result.abstained,
                "cited_indices": result.cited_indices,
                "retrieved_pages": result.retrieved_pages,
            },
            metadata={
                "eval_run_id": eval_run_id,
                "faithfulness_source": result.faithfulness_source,
                "reasons": result.reasons,
                "latency_ms": round(result.latency_ms, 2),
            },
        )
        comment = "; ".join(result.reasons) if result.reasons else None
        metrics: list[tuple[str, float]] = [("faithfulness", result.faithfulness)]
        if result.recall_at_k is not None:
            metrics.append(("recall_at_k", result.recall_at_k))
        if result.mrr is not None:
            metrics.append(("mrr", result.mrr))
        if result.keyword_coverage is not None:
            metrics.append(("keyword_coverage", result.keyword_coverage))
        if not result.answerable:
            metrics.append(("abstention_accuracy", 1.0 if result.abstained else 0.0))

        for metric_name, metric_val in metrics:
            score_fn = getattr(trace, "score", None)
            if callable(score_fn):
                score_fn(
                    name=metric_name,
                    value=metric_val,
                    data_type="NUMERIC",
                    comment=comment,
                )
            else:
                trace_id = getattr(trace, "id", None)
                resolved_client.score(
                    trace_id=trace_id,
                    name=metric_name,
                    value=metric_val,
                    data_type="NUMERIC",
                    comment=comment,
                )
    except Exception as exc:
        logger.warning("Failed to record Langfuse eval scores (fail-open): %s", exc)


async def _embed_texts_in_batches(
    llm_client: BaseLLMClient,
    texts: Sequence[str],
    batch_size: int,
) -> list[list[float]]:
    """Soru listesini embedding API sınırları içinde partiler halinde vektörleştirir."""
    if not texts:
        return []
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(await llm_client.embed_texts(texts=batch))
    return vectors


async def _resolve_live_document_ids(
    dataset: GoldenDataset,
    vector_db: BaseVectorDB,
) -> GoldenDataset:
    """Canlı Qdrant indeksinde UUID'si değişen dokümanları dosya adıyla eşler (fail-open)."""
    try:
        live_docs = await vector_db.list_documents(tenant_id=dataset.tenant_id)
    except Exception as exc:
        logger.warning(
            "Failed to list live documents for eval filename resolution (fail-open): %s",
            type(exc).__name__,
        )
        return dataset

    if not live_docs:
        return dataset

    live_ids = {doc.document_id for doc in live_docs}
    doc_id_by_name: dict[str, str] = {}
    doc_id_by_lower: dict[str, str] = {}
    for doc in live_docs:
        if doc.filename:
            doc_id_by_name.setdefault(doc.filename, doc.document_id)
            doc_id_by_lower.setdefault(Path(doc.filename).name.lower(), doc.document_id)

    filename_by_expected_id = {doc_id: fname for fname, doc_id in dataset.documents.items()}
    updated_items: list[GoldenEvalItem] = []
    remapped_docs = dict(dataset.documents)
    changed = False

    for item in dataset.items:
        if not item.answerable or item.expected_document_id in live_ids:
            updated_items.append(item)
            continue

        candidate_name = item.source_file or (
            filename_by_expected_id.get(item.expected_document_id)
            if item.expected_document_id
            else None
        )
        if not candidate_name:
            updated_items.append(item)
            continue

        basename = Path(candidate_name).name
        live_id = (
            doc_id_by_name.get(candidate_name)
            or doc_id_by_name.get(basename)
            or doc_id_by_lower.get(basename.lower())
        )
        if live_id and live_id != item.expected_document_id:
            logger.info(
                "Resolved drifted document_id for eval item=%s source_file=%s (%s -> %s)",
                item.id,
                candidate_name,
                item.expected_document_id,
                live_id,
            )
            remapped_docs[candidate_name] = live_id
            updated_items.append(item.model_copy(update={"expected_document_id": live_id}))
            changed = True
        else:
            updated_items.append(item)

    if not changed:
        return dataset
    return dataset.model_copy(update={"documents": remapped_docs, "items": updated_items})


async def evaluate_rag_dataset(
    *,
    dataset: GoldenDataset,
    llm_client: BaseLLMClient,
    vector_db: BaseVectorDB,
    llm_model: str,
    embedding_model: str,
    top_k: int = 5,
    score_threshold: float = 0.5,
    use_llm_judge: bool = False,
    sweep_thresholds: Sequence[float] | None = DEFAULT_THRESHOLDS,
    delay_seconds: float = 0.0,
    embedding_batch_size: int = 32,
    eval_run_id: str | None = None,
    langfuse_client: Any = None,
) -> EvalSummaryReport:
    """Altın veri seti üzerinde retrieval + generation + threshold sweep değerlendirmesi yapar."""
    if embedding_batch_size <= 0:
        raise ValueError("embedding_batch_size must be greater than 0")

    run_id = eval_run_id or f"eval-{utc_now().strftime('%Y%m%d-%H%M%S')}"
    resolved_dataset = await _resolve_live_document_ids(dataset, vector_db)
    questions = [item.question for item in resolved_dataset.items]
    query_vectors = await _embed_texts_in_batches(
        llm_client=llm_client,
        texts=questions,
        batch_size=embedding_batch_size,
    )

    sweep_items: list[ThresholdSweepItem] = []
    recommended_threshold: float | None = None
    if sweep_thresholds:
        sweep_items, recommended_threshold = await run_threshold_sweep(
            dataset=resolved_dataset,
            query_vectors=query_vectors,
            vector_db=vector_db,
            top_k=top_k,
            thresholds=sweep_thresholds,
        )

    results: list[QuestionEvalResult] = []
    for index, (item, vector) in enumerate(zip(resolved_dataset.items, query_vectors, strict=True)):
        if index > 0 and delay_seconds > 0.0:
            await asyncio.sleep(delay_seconds)

        t_start = time.perf_counter()
        scored_chunks = await vector_db.search(
            query_vector=vector,
            tenant_id=resolved_dataset.tenant_id,
            top_k=top_k,
            score_threshold=score_threshold,
        )
        recall, mrr = compute_retrieval_metrics(
            scored_chunks=scored_chunks,
            expected_document_id=item.expected_document_id if item.answerable else None,
            expected_pages=item.expected_pages,
            top_k=top_k,
        )

        messages = build_rag_messages(query=item.question, chunks=scored_chunks)
        answer = await llm_client.generate(messages=messages, temperature=0.0)
        abstained = is_abstention_answer(answer)
        cited_indices = extract_citation_indices(answer)
        keyword_cov = (
            compute_keyword_coverage(answer, item.expected_keywords) if item.answerable else None
        )
        faithfulness, reasons = compute_citation_faithfulness(
            answer=answer,
            answerable=item.answerable,
            retrieved_chunks=scored_chunks,
            expected_document_id=item.expected_document_id,
            expected_pages=item.expected_pages,
            expected_keywords=item.expected_keywords,
        )
        faithfulness_source = "citation_grounded"

        if use_llm_judge:
            if delay_seconds > 0.0:
                await asyncio.sleep(delay_seconds)
            faithfulness, reasons, faithfulness_source = await judge_faithfulness_with_llm(
                llm_client=llm_client,
                question=item.question,
                answer=answer,
                answerable=item.answerable,
                retrieved_chunks=scored_chunks,
                fallback_score=faithfulness,
                fallback_reasons=reasons,
            )

        elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
        question_result = QuestionEvalResult(
            id=item.id,
            category=item.category,
            question=item.question,
            answerable=item.answerable,
            expected_document_id=item.expected_document_id,
            expected_pages=list(item.expected_pages),
            retrieved_count=len(scored_chunks),
            retrieved_document_ids=[c.chunk.document_id for c in scored_chunks],
            retrieved_pages=[c.chunk.page_number for c in scored_chunks],
            top_score=round(scored_chunks[0].score, 4) if scored_chunks else None,
            recall_at_k=recall,
            mrr=mrr,
            cited_indices=cited_indices,
            keyword_coverage=keyword_cov,
            abstained=abstained,
            faithfulness=faithfulness,
            faithfulness_source=faithfulness_source,
            reasons=reasons,
            answer=answer,
            latency_ms=elapsed_ms,
        )
        results.append(question_result)
        record_eval_scores_to_langfuse(
            eval_run_id=run_id,
            tenant_id=dataset.tenant_id,
            result=question_result,
            score_threshold=score_threshold,
            client=langfuse_client,
        )

    recall_vals = [r.recall_at_k for r in results if r.recall_at_k is not None]
    mrr_vals = [r.mrr for r in results if r.mrr is not None]
    cov_vals = [r.keyword_coverage for r in results if r.keyword_coverage is not None]
    abstention_vals = [1.0 if r.abstained else 0.0 for r in results if not r.answerable]

    return EvalSummaryReport(
        eval_run_id=run_id,
        dataset_version=dataset.version,
        tenant_id=dataset.tenant_id,
        llm_model=llm_model,
        embedding_model=embedding_model,
        top_k=top_k,
        score_threshold=score_threshold,
        judge_mode="llm_judge" if use_llm_judge else "citation_grounded",
        total_questions=len(results),
        answerable_questions=len(recall_vals),
        unanswerable_questions=len(abstention_vals),
        mean_recall_at_k=_mean_or_zero(recall_vals),
        mean_mrr=_mean_or_zero(mrr_vals),
        mean_faithfulness=_mean_or_zero([r.faithfulness for r in results]),
        mean_keyword_coverage=_mean_or_zero(cov_vals),
        abstention_accuracy=_mean_or_zero(abstention_vals) if abstention_vals else 1.0,
        recommended_threshold=recommended_threshold,
        threshold_sweep=sweep_items,
        results=results,
        evaluated_at=utc_now().isoformat(),
    )
