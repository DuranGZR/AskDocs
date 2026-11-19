import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pymupdf
import pytest
from celery.exceptions import Retry
from qdrant_client import QdrantClient

from src.core.config import Settings, get_settings
from src.core.exceptions import ServiceUnavailableError
from src.services.base import (
    DocumentChunk,
    SyncBaseLLMClient,
    SyncBaseVectorDB,
    VectorPoint,
)
from src.services.llm_service import SyncOpenAICompatibleLLMClient
from src.services.qdrant_service import SyncQdrantService, build_chunk_point_id
from src.tasks.document_tasks import index_document_chunks, process_document_task


def _create_multipage_pdf(path: Path) -> None:
    """Batch vektörleştirme testleri için çok sayfalı gerçek PDF dosyası üretir."""
    with pymupdf.open() as doc:
        rect = pymupdf.Rect(50, 50, 540, 750)
        page1_text = " ".join(
            f"Birinci sayfa teknik şartname maddesi {idx} güvenli vektörleme testi."
            for idx in range(1, 8)
        )
        page2_text = " ".join(
            f"İkinci sayfa uyumluluk maddesi {idx} deterministik kimlik doğrulaması."
            for idx in range(1, 8)
        )
        for text in (page1_text, page2_text):
            page = doc.new_page()
            page.insert_textbox(rect, text)
        doc.save(path)


def test_worker_batches_embeddings_and_upserts_deterministic_points_to_qdrant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker chunk'ları batch embedding yapıp deterministik UUID5 ile Qdrant'a yazmalıdır."""
    pdf_path = tmp_path / "vector-batch.pdf"
    _create_multipage_pdf(pdf_path)

    monkeypatch.setenv("API_KEY", "test-api-key-minimum-8-chars")
    monkeypatch.setenv("CHUNK_SIZE", "120")
    monkeypatch.setenv("CHUNK_OVERLAP", "20")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("QDRANT_VECTOR_SIZE", "4")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "documents")

    batch_sizes: list[int] = []
    closed_resources: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://test-ai:11434/v1/embeddings"
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        batch_sizes.append(len(inputs))
        data = [
            {
                "object": "embedding",
                "index": idx,
                "embedding": [0.1 * (idx + 1), 0.2, 0.3, 0.4],
            }
            for idx, _ in enumerate(inputs)
        ]
        return httpx.Response(status_code=200, json={"object": "list", "data": data})

    sync_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    qdrant_client = QdrantClient(location=":memory:")

    class _SpyLLMClient(SyncOpenAICompatibleLLMClient):
        def close(self) -> None:
            closed_resources.append("llm_client")
            super().close()

    class _SpyVectorDB(SyncQdrantService):
        def close(self) -> None:
            closed_resources.append("vector_db")

    def _build_llm(settings: Settings) -> SyncBaseLLMClient:
        return _SpyLLMClient(
            base_url="http://test-ai:11434/v1",
            api_key=settings.LLM_API_KEY.get_secret_value(),
            embedding_model=settings.EMBEDDING_MODEL,
            client=sync_http_client,
        )

    def _build_vdb(settings: Settings) -> SyncBaseVectorDB:
        return _SpyVectorDB(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vector_size=settings.QDRANT_VECTOR_SIZE,
            client=qdrant_client,
        )

    monkeypatch.setattr("src.tasks.document_tasks.create_worker_llm_client", _build_llm)
    monkeypatch.setattr("src.tasks.document_tasks.create_worker_vector_db", _build_vdb)

    get_settings.cache_clear()
    try:
        result = process_document_task(
            document_id="doc-vec-1",
            file_path=str(pdf_path),
            tenant_id="tenant_alpha",
            file_type="pdf",
            request_id="req-vec-1",
        )

        chunk_count = int(str(result["chunk_count"]))
        assert result["status"] == "processed"
        assert result["file_exists"] is True
        assert result["page_count"] == 2
        assert chunk_count >= 4
        assert result["upserted_count"] == chunk_count
        assert not pdf_path.exists()

        assert len(batch_sizes) >= 2
        assert all(1 <= size <= 2 for size in batch_sizes)
        assert sum(batch_sizes) == chunk_count
        assert closed_resources == ["vector_db", "llm_client"]

        assert qdrant_client.count(collection_name="documents").count == chunk_count
        stored_points, _ = qdrant_client.scroll(
            collection_name="documents",
            limit=chunk_count + 10,
            with_payload=True,
            with_vectors=True,
        )
        assert len(stored_points) == chunk_count

        expected_ids = {build_chunk_point_id("doc-vec-1", idx) for idx in range(chunk_count)}
        actual_ids = {str(pt.id) for pt in stored_points}
        assert actual_ids == expected_ids

        pages_seen: set[int] = set()
        for pt in stored_points:
            payload = pt.payload or {}
            assert payload["tenant_id"] == "tenant_alpha"
            assert payload["document_id"] == "doc-vec-1"
            assert payload["file_type"] == "pdf"
            assert isinstance(payload["text"], str) and payload["text"]
            page_num = int(payload["page_number"])
            pages_seen.add(page_num)
            chunk_idx = int(payload["chunk_index"])
            assert str(pt.id) == build_chunk_point_id("doc-vec-1", chunk_idx)

        assert pages_seen == {1, 2}
    finally:
        get_settings.cache_clear()
        sync_http_client.close()
        qdrant_client.close()


def test_worker_idempotent_upsert_does_not_duplicate_points_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yarıda kesilip retry edilen görev aynı UUID5 kimlikleriyle mükerrer kayıt açmamalıdır."""
    sample_path = tmp_path / "retry-idempotent.txt"
    sample_path.write_text(
        " ".join(
            f"Tekrar deneme idempotency doğrulama cümlesi numarası {idx}." for idx in range(1, 10)
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("API_KEY", "test-api-key-minimum-8-chars")
    monkeypatch.setenv("CHUNK_SIZE", "110")
    monkeypatch.setenv("CHUNK_OVERLAP", "15")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("QDRANT_VECTOR_SIZE", "4")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "documents")

    embed_calls = 0

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal embed_calls
        embed_calls += 1
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        if embed_calls == 2:
            # İlk denemenin 2. batch'inde Ollama geçici olarak kesiliyor
            return httpx.Response(status_code=503, text="AI service temporarily unavailable")

        data = [
            {"object": "embedding", "index": idx, "embedding": [0.25, 0.25, 0.25, 0.25]}
            for idx, _ in enumerate(inputs)
        ]
        return httpx.Response(status_code=200, json={"object": "list", "data": data})

    sync_http_client = httpx.Client(transport=httpx.MockTransport(flaky_handler))
    qdrant_client = QdrantClient(location=":memory:")

    class _PersistentVectorDB(SyncQdrantService):
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.tasks.document_tasks.create_worker_llm_client",
        lambda settings: SyncOpenAICompatibleLLMClient(
            base_url="http://test-ai:11434/v1",
            embedding_model=settings.EMBEDDING_MODEL,
            client=sync_http_client,
        ),
    )
    monkeypatch.setattr(
        "src.tasks.document_tasks.create_worker_vector_db",
        lambda settings: _PersistentVectorDB(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vector_size=settings.QDRANT_VECTOR_SIZE,
            client=qdrant_client,
        ),
    )

    get_settings.cache_clear()
    try:
        # 1. Deneme: Batch 1 yazılır, Batch 2'de 503 gelince retry tetiklenir ve dosya korunur
        process_document_task.push_request(retries=0)
        try:
            with pytest.raises((Retry, ServiceUnavailableError)):
                process_document_task(
                    document_id="doc-retry-idem",
                    file_path=str(sample_path),
                    tenant_id="tenant_retry",
                    file_type="txt",
                    request_id="req-retry-1",
                )
        finally:
            process_document_task.pop_request()

        assert sample_path.exists()
        assert qdrant_client.count(collection_name="documents").count == 2

        # 2. Deneme (Retry): Tüm batch'ler yeniden işlenir, Batch 1 üzerine yazar (0 mükerrer)
        process_document_task.push_request(retries=1)
        try:
            final_result = process_document_task(
                document_id="doc-retry-idem",
                file_path=str(sample_path),
                tenant_id="tenant_retry",
                file_type="txt",
                request_id="req-retry-1",
            )
        finally:
            process_document_task.pop_request()

        total_chunks = int(str(final_result["chunk_count"]))
        assert final_result["status"] == "processed"
        assert total_chunks >= 3
        assert final_result["upserted_count"] == total_chunks
        assert not sample_path.exists()
        assert qdrant_client.count(collection_name="documents").count == total_chunks
    finally:
        get_settings.cache_clear()
        sync_http_client.close()
        qdrant_client.close()


def test_index_document_chunks_edge_cases() -> None:
    """Boş girdi, geçersiz batch_size ve eksik vektör sayısı güvenli şekilde ele alınmalıdır."""

    class _MockLLM(SyncBaseLLMClient):
        def __init__(self, vectors_to_return: list[list[float]]) -> None:
            self.called = False
            self._vectors = vectors_to_return

        def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
            self.called = True
            return self._vectors

        def close(self) -> None:
            return None

    class _MockVectorDB(SyncBaseVectorDB):
        def __init__(self) -> None:
            self.ensured = False

        def ensure_collection(self) -> None:
            self.ensured = True

        def upsert(self, *, points: Sequence[VectorPoint]) -> int:
            return len(points)

        def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
            return None

        def close(self) -> None:
            return None

    llm = _MockLLM(vectors_to_return=[[0.1, 0.2]])
    vdb = _MockVectorDB()

    assert index_document_chunks(chunks=[], llm_client=llm, vector_db=vdb) == 0
    assert not llm.called
    assert not vdb.ensured

    sample_chunks = [
        DocumentChunk(
            document_id="doc-edge",
            tenant_id="tenant_edge",
            page_number=1,
            chunk_index=0,
            text="Birinci parça.",
            file_type="txt",
        ),
        DocumentChunk(
            document_id="doc-edge",
            tenant_id="tenant_edge",
            page_number=1,
            chunk_index=1,
            text="İkinci parça.",
            file_type="txt",
        ),
    ]

    with pytest.raises(ValueError, match="batch_size"):
        index_document_chunks(chunks=sample_chunks, llm_client=llm, vector_db=vdb, batch_size=0)

    with pytest.raises(ServiceUnavailableError, match="mismatch"):
        index_document_chunks(chunks=sample_chunks, llm_client=llm, vector_db=vdb, batch_size=2)


def test_worker_rolls_back_partial_vectors_on_permanent_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kalıcı hata veya zaman aşımında Qdrant'a yazılmış kısmi vektörler geri silinmelidir."""
    from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

    sample_path = tmp_path / "partial-rollback.txt"
    sample_path.write_text(
        " ".join(f"Kısmi vektör geri alma testi cümle numarası {idx}." for idx in range(1, 10)),
        encoding="utf-8",
    )

    monkeypatch.setenv("API_KEY", "test-api-key-minimum-8-chars")
    monkeypatch.setenv("CHUNK_SIZE", "110")
    monkeypatch.setenv("CHUNK_OVERLAP", "15")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("QDRANT_VECTOR_SIZE", "4")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "documents")

    embed_calls = 0

    def timeout_on_second_batch(request: httpx.Request) -> httpx.Response:
        nonlocal embed_calls
        embed_calls += 1
        if embed_calls == 2:
            raise SoftTimeLimitExceeded()
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        data = [
            {"object": "embedding", "index": idx, "embedding": [0.5, 0.5, 0.5, 0.5]}
            for idx, _ in enumerate(inputs)
        ]
        return httpx.Response(status_code=200, json={"object": "list", "data": data})

    sync_http_client = httpx.Client(transport=httpx.MockTransport(timeout_on_second_batch))
    qdrant_client = QdrantClient(location=":memory:")

    class _PersistentVectorDB(SyncQdrantService):
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.tasks.document_tasks.create_worker_llm_client",
        lambda settings: SyncOpenAICompatibleLLMClient(
            base_url="http://test-ai:11434/v1",
            embedding_model=settings.EMBEDDING_MODEL,
            client=sync_http_client,
        ),
    )
    monkeypatch.setattr(
        "src.tasks.document_tasks.create_worker_vector_db",
        lambda settings: _PersistentVectorDB(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vector_size=settings.QDRANT_VECTOR_SIZE,
            client=qdrant_client,
        ),
    )

    get_settings.cache_clear()
    try:
        process_document_task.push_request(retries=0)
        try:
            with pytest.raises(SoftTimeLimitExceeded):
                process_document_task(
                    document_id="doc-partial-timeout",
                    file_path=str(sample_path),
                    tenant_id="tenant_rollback",
                    file_type="txt",
                    request_id="req-rollback-1",
                )
        finally:
            process_document_task.pop_request()

        assert qdrant_client.count(collection_name="documents").count == 0
        assert not sample_path.exists()
    finally:
        get_settings.cache_clear()
        sync_http_client.close()
        qdrant_client.close()


def test_worker_aborts_and_rolls_back_on_daily_quota_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Günlük API kotası dolduğunda worker beklemeden durmalı ve kısmi vektörleri silmelidir."""
    sample_path = tmp_path / "daily-quota.txt"
    sample_path.write_text(
        " ".join(f"Günlük kota kontrol testi cümle numarası {idx}." for idx in range(1, 10)),
        encoding="utf-8",
    )

    monkeypatch.setenv("API_KEY", "test-api-key-minimum-8-chars")
    monkeypatch.setenv("CHUNK_SIZE", "110")
    monkeypatch.setenv("CHUNK_OVERLAP", "15")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("QDRANT_VECTOR_SIZE", "4")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "documents")

    embed_calls = 0
    sleep_calls: list[float] = []

    def quota_on_second_batch(request: httpx.Request) -> httpx.Response:
        nonlocal embed_calls
        embed_calls += 1
        if embed_calls == 2:
            return httpx.Response(
                status_code=429,
                json={
                    "error": {
                        "code": 429,
                        "message": (
                            "Quota exceeded, limit: 1000, description: "
                            "Embed content requests per day limit exceeded on Free Tier.\n"
                            "Please retry in 15h22m46s."
                        ),
                    }
                },
            )
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        data = [
            {"object": "embedding", "index": idx, "embedding": [0.4, 0.4, 0.4, 0.4]}
            for idx, _ in enumerate(inputs)
        ]
        return httpx.Response(status_code=200, json={"object": "list", "data": data})

    monkeypatch.setattr("src.services.llm_service.time.sleep", sleep_calls.append)
    sync_http_client = httpx.Client(transport=httpx.MockTransport(quota_on_second_batch))
    qdrant_client = QdrantClient(location=":memory:")

    class _PersistentVectorDB(SyncQdrantService):
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.tasks.document_tasks.create_worker_llm_client",
        lambda settings: SyncOpenAICompatibleLLMClient(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            embedding_model=settings.EMBEDDING_MODEL,
            client=sync_http_client,
        ),
    )
    monkeypatch.setattr(
        "src.tasks.document_tasks.create_worker_vector_db",
        lambda settings: _PersistentVectorDB(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vector_size=settings.QDRANT_VECTOR_SIZE,
            client=qdrant_client,
        ),
    )

    get_settings.cache_clear()
    try:
        result = process_document_task(
            document_id="doc-daily-quota",
            file_path=str(sample_path),
            tenant_id="tenant_quota",
            file_type="txt",
            request_id="req-quota-1",
        )
        assert result["status"] == "quota_exhausted"
        assert result["error"] == (
            "❌ Google Gemini API günlük ücretsiz kotası (1.000 parça/gün) doldu."
        )
        assert result["upserted_count"] == 0
        assert sleep_calls == []
        assert qdrant_client.count(collection_name="documents").count == 0
        assert not sample_path.exists()
    finally:
        get_settings.cache_clear()
        sync_http_client.close()
        qdrant_client.close()
