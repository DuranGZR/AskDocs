from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from src.evals.rag_eval import (
    GoldenDataset,
    GoldenEvalItem,
    compute_citation_faithfulness,
    compute_keyword_coverage,
    compute_retrieval_metrics,
    evaluate_rag_dataset,
    extract_citation_indices,
    is_abstention_answer,
    judge_faithfulness_with_llm,
    load_golden_dataset,
)
from src.services.base import (
    BaseLLMClient,
    BaseVectorDB,
    ChatMessage,
    DocumentChunk,
    IndexedDocumentSummary,
    ScoredChunk,
    VectorPoint,
)


class _FakeLangfuseScoreClient:
    def __init__(self, *, fail_score: bool = False) -> None:
        self.traces: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self._fail_score = fail_score

    def trace(self, **kwargs: Any) -> Any:
        trace_id = f"trace-{len(self.traces) + 1}"
        entry = {"id": trace_id, **kwargs}
        self.traces.append(entry)
        parent = self

        class _TraceHandle:
            id = trace_id

            def score(self, **score_kwargs: Any) -> None:
                if parent._fail_score:
                    raise RuntimeError("Langfuse score error")
                parent.scores.append({"trace_id": trace_id, **score_kwargs})

        return _TraceHandle()


class _StubEvalLLMClient(BaseLLMClient):
    def __init__(self, *, judge_response: str | None = None, fail_judge: bool = False) -> None:
        self._judge_response = judge_response
        self._fail_judge = fail_judge

    async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            if "yemek" in text.lower():
                vectors.append([0.0, 1.0])
            else:
                vectors.append([1.0, 0.0])
        return vectors

    async def generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        system = messages[0].content
        user = messages[-1].content
        if "faithfulness evaluator" in system.lower():
            if self._fail_judge:
                raise RuntimeError("Judge endpoint failed")
            return self._judge_response or '{"score": 0.95, "reason": "Grounded in context"}'
        if "yemek" in user.lower():
            return "Sağlanan belgelerde aylık yemek kartı hakkında bilgi bulunmamaktadır."
        return "Standart haftalık çalışma süresi 40 saattir [Source 1]."

    async def stream_generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield ""

    async def close(self) -> None:
        return None


class _StubEvalVectorDB(BaseVectorDB):
    async def ensure_collection(self) -> None:
        return None

    async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
        return len(points)

    async def search(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        top_k: int = 5,
        score_threshold: float = 0.5,
    ) -> list[ScoredChunk]:
        # Negatif sorgu vektörü düşük skorla dönsün ki eşik taraması gürültüyü yakalasın
        if query_vector[1] > query_vector[0]:
            if score_threshold <= 0.40:
                chunk = DocumentChunk(
                    document_id="doc-hr",
                    tenant_id=tenant_id,
                    page_number=2,
                    chunk_index=0,
                    text="Ev ofisi desteği 15.000 TL'dir.",
                    file_type="pdf",
                )
                return [ScoredChunk(point_id="neg-1", score=0.38, chunk=chunk)]
            return []

        if score_threshold > 0.65:
            return []

        hit_chunk = DocumentChunk(
            document_id="doc-hr",
            tenant_id=tenant_id,
            page_number=1,
            chunk_index=0,
            text="Haftalık çalışma süresi 40 saattir.",
            file_type="pdf",
        )
        other_chunk = DocumentChunk(
            document_id="doc-other",
            tenant_id=tenant_id,
            page_number=4,
            chunk_index=0,
            text="Other text.",
            file_type="pdf",
        )
        return [
            ScoredChunk(point_id="p-other", score=0.64, chunk=other_chunk),
            ScoredChunk(point_id="p-hit", score=0.62, chunk=hit_chunk),
        ][:top_k]

    async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _make_scored(doc_id: str, page: int, score: float = 0.85) -> ScoredChunk:
    return ScoredChunk(
        point_id=f"{doc_id}-{page}",
        score=score,
        chunk=DocumentChunk(
            document_id=doc_id,
            tenant_id="test-user",
            page_number=page,
            chunk_index=0,
            text="Sample chunk text",
            file_type="pdf",
        ),
    )


def test_load_repo_golden_dataset_validates_all_items() -> None:
    """Depodaki altın veri seti en az 20 soru ve pozitif/negatif dağılımla geçerli olmalıdır."""
    dataset = load_golden_dataset(Path("evals/golden_dataset.json"))
    assert len(dataset.items) >= 20
    assert any(item.answerable for item in dataset.items)
    assert any(not item.answerable for item in dataset.items)
    assert len({item.id for item in dataset.items}) == len(dataset.items)


def test_retrieval_metrics_recall_at_k_and_mrr() -> None:
    """Recall@K ve MRR beklenen doküman/sayfa sırasına göre doğru hesaplanmalıdır."""
    chunks = [_make_scored("doc-b", 1, 0.9), _make_scored("doc-a", 3, 0.8)]

    recall, mrr = compute_retrieval_metrics(
        scored_chunks=chunks,
        expected_document_id="doc-a",
        expected_pages=[3],
        top_k=5,
    )
    assert recall == 1.0
    assert mrr == 0.5

    miss_recall, miss_mrr = compute_retrieval_metrics(
        scored_chunks=chunks,
        expected_document_id="doc-a",
        expected_pages=[2],
        top_k=5,
    )
    assert miss_recall == 0.0
    assert miss_mrr == 0.0

    neg_recall, neg_mrr = compute_retrieval_metrics(
        scored_chunks=chunks,
        expected_document_id=None,
        expected_pages=[],
        top_k=5,
    )
    assert neg_recall is None
    assert neg_mrr is None


def test_citation_extraction_and_turkish_keyword_coverage() -> None:
    """Atıf numaraları ve Türkçe İ/ligatür normalizasyonlu anahtar kelime kapsama çalışmalıdır."""
    assert extract_citation_indices("Bakınız [Source 2] ve [Source 1, Source 3].") == [2, 1, 3]
    assert extract_citation_indices("Birleşik [source 4, 2] formatı.") == [4, 2]

    cov = compute_keyword_coverage(
        "İşe yeni başlayanlara Ev Oﬁsi kurulum desteği 15.000 TL'dir.",
        ["ev ofisi", "15.000", "iş kanıtı|kurulum"],
    )
    assert cov == 1.0
    assert is_abstention_answer("Sağlanan belgelerde bu konuda bilgi bulunmamaktadır.")
    assert not is_abstention_answer("Tek seferlik 15.000 TL destek verilir [Source 1].")


def test_citation_faithfulness_handles_positive_phantom_and_negative_cases() -> None:
    """Faithfulness; doğru atıf, hayali atıf, gereksiz ret ve negatif kontrolü ayırmalıdır."""
    chunks = [_make_scored("doc-hr", 1), _make_scored("doc-hr", 2)]

    good_score, _ = compute_citation_faithfulness(
        answer="Haftalık 40 saat çalışılır [Source 1].",
        answerable=True,
        retrieved_chunks=chunks,
        expected_document_id="doc-hr",
        expected_pages=[1],
        expected_keywords=["40 saat"],
    )
    assert good_score == 1.0

    phantom_score, phantom_reasons = compute_citation_faithfulness(
        answer="Haftalık 40 saat çalışılır [Source 9].",
        answerable=True,
        retrieved_chunks=chunks,
        expected_document_id="doc-hr",
        expected_pages=[1],
        expected_keywords=["40 saat"],
    )
    assert phantom_score < good_score
    assert any("phantom" in r.lower() for r in phantom_reasons)

    false_refusal_score, _ = compute_citation_faithfulness(
        answer="Sağlanan belgelerde bu bilgi bulunmamaktadır.",
        answerable=True,
        retrieved_chunks=chunks,
        expected_document_id="doc-hr",
        expected_pages=[1],
        expected_keywords=["40 saat"],
    )
    assert false_refusal_score == 0.0

    neg_ok, _ = compute_citation_faithfulness(
        answer="The provided documents do not contain information about meal cards.",
        answerable=False,
        retrieved_chunks=[],
        expected_document_id=None,
        expected_pages=[],
        expected_keywords=[],
    )
    assert neg_ok == 1.0

    neg_hallucination, _ = compute_citation_faithfulness(
        answer="Aylık 5.000 TL yemek kartı yatırılır [Source 1].",
        answerable=False,
        retrieved_chunks=chunks,
        expected_document_id=None,
        expected_pages=[],
        expected_keywords=[],
    )
    assert neg_hallucination == 0.0


async def test_llm_judge_fallback_and_full_eval_pipeline_with_langfuse() -> None:
    """Tam eval akışı sweep hesaplamalı, Langfuse score yazmalı ve judge hatasında dönmelidir."""
    fallback_score, reasons, source = await judge_faithfulness_with_llm(
        llm_client=_StubEvalLLMClient(fail_judge=True),
        question="Soru",
        answer="Cevap [Source 1]",
        answerable=True,
        retrieved_chunks=[_make_scored("doc-hr", 1)],
        fallback_score=0.8,
        fallback_reasons=["baseline"],
    )
    assert fallback_score == 0.8
    assert source == "citation_fallback"
    assert any("fallback" in r.lower() for r in reasons)

    dataset = GoldenDataset(
        tenant_id="tenant-eval",
        documents={"hr.pdf": "doc-hr"},
        items=[
            GoldenEvalItem(
                id="t1",
                question="Haftalık çalışma süresi kaç saattir?",
                answerable=True,
                source_file="hr.pdf",
                expected_pages=[1],
                reference_answer="40 saattir",
                expected_keywords=["40"],
            ),
            GoldenEvalItem(
                id="t2",
                question="Yemek kartı kaç TL?",
                answerable=False,
                reference_answer="Bilgi yok",
                expected_keywords=[],
            ),
        ],
    )
    fake_lf = _FakeLangfuseScoreClient()
    report = await evaluate_rag_dataset(
        dataset=dataset,
        llm_client=_StubEvalLLMClient(),
        vector_db=_StubEvalVectorDB(),
        llm_model="stub-llm",
        embedding_model="stub-emb",
        top_k=5,
        score_threshold=0.50,
        use_llm_judge=True,
        sweep_thresholds=(0.35, 0.50, 0.70),
        langfuse_client=fake_lf,
    )

    assert report.mean_recall_at_k == 1.0
    assert report.mean_mrr == 0.5
    assert report.abstention_accuracy == 1.0
    assert report.mean_faithfulness == 0.95
    assert report.recommended_threshold == 0.50
    assert len(report.threshold_sweep) == 3
    assert report.threshold_sweep[0].negative_noise_rate == 1.0
    assert report.threshold_sweep[1].negative_noise_rate == 0.0
    assert len(fake_lf.traces) == 2
    assert any(s["name"] == "recall_at_k" and s["value"] == 1.0 for s in fake_lf.scores)
    assert any(s["name"] == "mrr" and s["value"] == 0.5 for s in fake_lf.scores)
    assert any(s["name"] == "faithfulness" and s["value"] == 0.95 for s in fake_lf.scores)

    # Langfuse score hatasında da fail-open kalmalıdır
    broken_lf = _FakeLangfuseScoreClient(fail_score=True)
    report_fail_open = await evaluate_rag_dataset(
        dataset=dataset,
        llm_client=_StubEvalLLMClient(),
        vector_db=_StubEvalVectorDB(),
        llm_model="stub-llm",
        embedding_model="stub-emb",
        sweep_thresholds=None,
        langfuse_client=broken_lf,
    )
    assert report_fail_open.total_questions == 2


async def test_evaluate_rag_dataset_batches_embeddings_and_resolves_live_filenames() -> None:
    """Embed çağrıları partilenmeli ve güncel doküman ID'si dosya adından çözülmelidir."""

    class _BatchSpyLLMClient(_StubEvalLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.embed_batch_sizes: list[int] = []

        async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
            self.embed_batch_sizes.append(len(texts))
            return await super().embed_texts(texts=texts)

    class _ReuploadedVectorDB(_StubEvalVectorDB):
        async def list_documents(self, *, tenant_id: str) -> list[IndexedDocumentSummary]:
            return [
                IndexedDocumentSummary(
                    document_id="doc-hr-reuploaded-999",
                    tenant_id=tenant_id,
                    filename="hr.pdf",
                    file_type="pdf",
                    page_count=2,
                    chunk_count=3,
                )
            ]

        async def search(
            self,
            *,
            query_vector: Sequence[float],
            tenant_id: str,
            top_k: int = 5,
            score_threshold: float = 0.5,
        ) -> list[ScoredChunk]:
            del query_vector, top_k, score_threshold
            chunk = DocumentChunk(
                document_id="doc-hr-reuploaded-999",
                tenant_id=tenant_id,
                page_number=1,
                chunk_index=0,
                text="Haftalık çalışma süresi 40 saattir.",
                file_type="pdf",
                filename="hr.pdf",
            )
            return [ScoredChunk(point_id="p-live-1", score=0.92, chunk=chunk)]

    dataset = GoldenDataset(
        tenant_id="tenant-eval",
        documents={"hr.pdf": "doc-hr-stale-001"},
        items=[
            GoldenEvalItem(
                id=f"q{i}",
                question=f"Haftalık çalışma süresi {i} nedir?",
                answerable=True,
                source_file="hr.pdf",
                expected_pages=[1],
                reference_answer="40 saattir",
                expected_keywords=["40"],
            )
            for i in range(1, 6)
        ],
    )

    llm_client = _BatchSpyLLMClient()
    report = await evaluate_rag_dataset(
        dataset=dataset,
        llm_client=llm_client,
        vector_db=_ReuploadedVectorDB(),
        llm_model="stub-llm",
        embedding_model="stub-emb",
        top_k=3,
        score_threshold=0.5,
        sweep_thresholds=(0.5,),
        embedding_batch_size=2,
    )

    assert llm_client.embed_batch_sizes == [2, 2, 1]
    assert report.mean_recall_at_k == 1.0
    assert report.mean_mrr == 1.0
    assert all(res.expected_document_id == "doc-hr-reuploaded-999" for res in report.results)
    assert dataset.items[0].expected_document_id == "doc-hr-stale-001"


def test_is_abstention_answer_detects_is_not_mentioned_phrasing() -> None:
    """Kapsam dışı sorularda İngilizce/Türkçe doğal ret kalıpları geçerli sayılmalıdır."""
    assert is_abstention_answer(
        "Based on the provided documents, the WMT-19 BLEU-4 score is not mentioned."
    )
    assert is_abstention_answer("Bu bilgi mevcut kaynaklarda yer almamaktadır.")
    assert not is_abstention_answer("Table 1 reports that FiD achieves 51.4 EM on NQ.")
