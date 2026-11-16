from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient, QdrantClient, models

from src.api.dependencies import get_vector_db
from src.core.config import Settings
from src.main import lifespan
from src.services.base import DocumentChunk, VectorPoint
from src.services.qdrant_service import (
    AsyncQdrantService,
    SyncQdrantService,
    build_chunk_point_id,
)

# Bellek içi (:memory:) Qdrant modunda payload index uyarısını test çıktısında sustur
pytestmark = pytest.mark.filterwarnings(
    "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
)


def _make_point(
    *,
    document_id: str,
    tenant_id: str,
    chunk_index: int,
    text: str,
    vector: list[float],
    page_number: int = 1,
) -> VectorPoint:
    """Testler için deterministik ID'li VectorPoint nesnesi üretir."""
    return VectorPoint(
        point_id=build_chunk_point_id(document_id, chunk_index),
        vector=vector,
        chunk=DocumentChunk(
            document_id=document_id,
            tenant_id=tenant_id,
            page_number=page_number,
            chunk_index=chunk_index,
            text=text,
            file_type="pdf",
        ),
    )


def test_qdrant_settings_validation() -> None:
    """Qdrant ayarları geçerli URL/boyut kabul etmeli, bozuk değerleri reddetmelidir."""
    valid = Settings(
        _env_file=None,
        API_KEY="test-api-key-minimum-8-chars",
        QDRANT_URL="  http://qdrant:6333  ",
        QDRANT_COLLECTION_NAME="corp_docs_1",
        QDRANT_VECTOR_SIZE=768,
    )
    assert valid.QDRANT_URL == "http://qdrant:6333"
    assert valid.QDRANT_COLLECTION_NAME == "corp_docs_1"
    assert valid.QDRANT_VECTOR_SIZE == 768

    for bad_url in ("redis://localhost:6333", "http://", "http://localhost:notaport"):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                API_KEY="test-api-key-minimum-8-chars",
                QDRANT_URL=bad_url,
            )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            API_KEY="test-api-key-minimum-8-chars",
            QDRANT_COLLECTION_NAME="invalid collection!",
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            API_KEY="test-api-key-minimum-8-chars",
            QDRANT_VECTOR_SIZE=0,
        )


def test_build_chunk_point_id_is_deterministic_uuid5() -> None:
    """Aynı doküman ve chunk_index her zaman aynı UUID5 değerini üretmelidir."""
    first = build_chunk_point_id("doc-alpha", 0)
    second = build_chunk_point_id("doc-alpha", 0)
    different_chunk = build_chunk_point_id("doc-alpha", 1)
    different_doc = build_chunk_point_id("doc-beta", 0)

    assert first == second
    assert first != different_chunk
    assert first != different_doc

    with pytest.raises(ValueError, match="Either 'url' or 'client'"):
        AsyncQdrantService(collection_name="documents", vector_size=4)

    with pytest.raises(ValueError, match="Either 'url' or 'client'"):
        SyncQdrantService(collection_name="documents", vector_size=4)


async def test_async_qdrant_service_upsert_search_and_multi_tenant_isolation() -> None:
    """AsyncQdrantService idempotent upsert, eşik skoru ve tenant izolasyonunu sağlamalıdır."""
    client = AsyncQdrantClient(location=":memory:")
    service = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=client,
    )
    try:
        # İki kez çağrıldığında hata vermeden idempotent çalışmalı
        await service.ensure_collection()
        await service.ensure_collection()

        assert await service.upsert(points=[]) == 0

        points = [
            _make_point(
                document_id="doc-a",
                tenant_id="tenant_a",
                chunk_index=0,
                text="İlk taslak metin",
                vector=[1.0, 0.0, 0.0, 0.0],
            ),
            _make_point(
                document_id="doc-a",
                tenant_id="tenant_a",
                chunk_index=1,
                text="Düşük benzerlikli alakasız parça",
                vector=[0.4, 0.9, 0.0, 0.0],
            ),
            _make_point(
                document_id="doc-b",
                tenant_id="tenant_b",
                chunk_index=0,
                text="Başka şirketin gizli sözleşmesi",
                vector=[1.0, 0.0, 0.0, 0.0],
            ),
        ]
        written = await service.upsert(points=points)
        assert written == 3

        # Aynı (doc-a, chunk 0) tekrar upsert edildiğinde mükerrer kayıt açmadan güncellemeli
        updated_point = _make_point(
            document_id="doc-a",
            tenant_id="tenant_a",
            chunk_index=0,
            text="Güncellenmiş idempotent metin",
            vector=[1.0, 0.0, 0.0, 0.0],
        )
        await service.upsert(points=[updated_point])

        hits_a = await service.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            tenant_id="tenant_a",
            top_k=5,
            score_threshold=0.8,
        )
        assert len(hits_a) == 1
        assert hits_a[0].chunk.tenant_id == "tenant_a"
        assert hits_a[0].chunk.document_id == "doc-a"
        assert hits_a[0].chunk.text == "Güncellenmiş idempotent metin"
        assert hits_a[0].score >= 0.99

        # tenant_b kendi dokümanını silebilmeli ama tenant_a'nın dokümanını silememeli
        await service.delete_by_document(tenant_id="tenant_b", document_id="doc-a")
        still_there = await service.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            tenant_id="tenant_a",
            top_k=5,
            score_threshold=0.1,
        )
        assert len(still_there) == 2

        await service.delete_by_document(tenant_id="tenant_a", document_id="doc-a")
        after_delete = await service.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            tenant_id="tenant_a",
            top_k=5,
            score_threshold=0.1,
        )
        assert after_delete == []

        # tenant_b'nin kendi verisi ise hala duruyor olmalı
        hits_b = await service.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            tenant_id="tenant_b",
            top_k=5,
            score_threshold=0.8,
        )
        assert len(hits_b) == 1
        assert hits_b[0].chunk.tenant_id == "tenant_b"
    finally:
        await service.close()


def test_sync_qdrant_service_lifecycle_and_payload_index_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SyncQdrantService indeksleri kurmalı, senkron upsert ve silme yapabilmelidir."""
    client = QdrantClient(location=":memory:")
    indexed_fields: list[tuple[str, models.PayloadSchemaType]] = []
    orig_create_index = client.create_payload_index

    def _spy_create_index(
        collection_name: str,
        field_name: str,
        field_schema: models.PayloadSchemaType | None = None,
        **kwargs: object,
    ) -> object:
        if isinstance(field_schema, models.PayloadSchemaType):
            indexed_fields.append((field_name, field_schema))
        return orig_create_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
        )

    monkeypatch.setattr(client, "create_payload_index", _spy_create_index)

    service = SyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=client,
    )
    try:
        service.ensure_collection()
        assert indexed_fields == [
            ("tenant_id", models.PayloadSchemaType.KEYWORD),
            ("document_id", models.PayloadSchemaType.KEYWORD),
        ]

        assert service.upsert(points=[]) == 0
        point = _make_point(
            document_id="doc-sync-1",
            tenant_id="tenant_sync",
            chunk_index=0,
            text="Worker senkron kayıt testi",
            vector=[0.5, 0.5, 0.0, 0.0],
        )
        assert service.upsert(points=[point]) == 1
        assert client.count(collection_name="documents").count == 1

        service.delete_by_document(tenant_id="tenant_sync", document_id="doc-sync-1")
        assert client.count(collection_name="documents").count == 0
    finally:
        service.close()


async def test_lifespan_initializes_and_closes_vector_db(
    app: FastAPI,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uygulama lifespan'i açılışta ensure_collection, kapanışta close çağırmalıdır."""
    events: list[str] = []

    class _FakeAsyncQdrantService(AsyncQdrantService):
        def __init__(self, **kwargs: object) -> None:
            events.append("init")

        async def ensure_collection(self) -> None:
            events.append("ensure_collection")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("src.main.AsyncQdrantService", _FakeAsyncQdrantService)

    async with lifespan(app):
        assert events == ["init", "ensure_collection"]
        from starlette.requests import Request

        scope = {"type": "http", "app": app, "headers": []}
        req = Request(scope)
        resolved = get_vector_db(req, test_settings)
        assert resolved is app.state.vector_db

    assert events == ["init", "ensure_collection", "close"]


async def test_async_qdrant_service_list_documents_aggregates_and_isolates_tenants() -> None:
    """list_documents sayfaları/parçaları saymalı, filename dönmeli ve tenant'ı izole etmelidir."""
    client = AsyncQdrantClient(location=":memory:")
    service = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=client,
    )
    try:
        await service.ensure_collection()
        await service.upsert(
            points=[
                VectorPoint(
                    point_id=build_chunk_point_id("doc-policy", 0),
                    vector=[1.0, 0.0, 0.0, 0.0],
                    chunk=DocumentChunk(
                        document_id="doc-policy",
                        tenant_id="tenant_a",
                        page_number=1,
                        chunk_index=0,
                        text="Sayfa 1 metni",
                        file_type="pdf",
                        filename="el_kitabi.pdf",
                    ),
                ),
                VectorPoint(
                    point_id=build_chunk_point_id("doc-policy", 1),
                    vector=[0.9, 0.1, 0.0, 0.0],
                    chunk=DocumentChunk(
                        document_id="doc-policy",
                        tenant_id="tenant_a",
                        page_number=3,
                        chunk_index=1,
                        text="Sayfa 3 metni",
                        file_type="pdf",
                        filename="el_kitabi.pdf",
                    ),
                ),
                VectorPoint(
                    point_id=build_chunk_point_id("doc-policy", 2),
                    vector=[0.8, 0.2, 0.0, 0.0],
                    chunk=DocumentChunk(
                        document_id="doc-policy",
                        tenant_id="tenant_a",
                        page_number=3,
                        chunk_index=2,
                        text="Sayfa 3 ikinci parça",
                        file_type="pdf",
                        filename="el_kitabi.pdf",
                    ),
                ),
                VectorPoint(
                    point_id=build_chunk_point_id("doc-legacy-1234", 0),
                    vector=[0.7, 0.3, 0.0, 0.0],
                    chunk=DocumentChunk(
                        document_id="doc-legacy-1234",
                        tenant_id="tenant_a",
                        page_number=1,
                        chunk_index=0,
                        text="Eski kayıt metni",
                        file_type="txt",
                        filename="",
                    ),
                ),
                VectorPoint(
                    point_id=build_chunk_point_id("doc-other-tenant", 0),
                    vector=[1.0, 0.0, 0.0, 0.0],
                    chunk=DocumentChunk(
                        document_id="doc-other-tenant",
                        tenant_id="tenant_b",
                        page_number=1,
                        chunk_index=0,
                        text="Gizli belge",
                        file_type="pdf",
                        filename="gizli.pdf",
                    ),
                ),
            ]
        )

        docs_a = await service.list_documents(tenant_id="tenant_a")
        assert [(d.document_id, d.filename, d.page_count, d.chunk_count) for d in docs_a] == [
            ("doc-legacy-1234", "doc-lega.txt", 1, 1),
            ("doc-policy", "el_kitabi.pdf", 2, 3),
        ]

        search_hits = await service.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            tenant_id="tenant_a",
            top_k=1,
            score_threshold=0.9,
        )
        assert len(search_hits) == 1
        assert search_hits[0].chunk.filename == "el_kitabi.pdf"
        assert await service.list_tenants() == ["tenant_a", "tenant_b"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_list_documents_projects_metadata_and_list_tenants_falls_back_from_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_documents text payload çekmemeli; list_tenants facet hatasında scroll'a dönmelidir."""
    client = AsyncQdrantClient(location=":memory:")
    service = AsyncQdrantService(
        collection_name="documents",
        vector_size=4,
        client=client,
    )
    scroll_with_payload_calls: list[object] = []
    orig_scroll = client.scroll

    async def _spy_scroll(**kwargs: Any) -> Any:
        scroll_with_payload_calls.append(kwargs.get("with_payload"))
        return await orig_scroll(**kwargs)

    async def _failing_facet(**kwargs: Any) -> Any:
        raise NotImplementedError("facet not supported")

    try:
        await service.ensure_collection()
        await service.upsert(
            points=[
                _make_point(
                    document_id="doc-meta-1",
                    tenant_id="tenant_meta",
                    chunk_index=0,
                    text="Çok uzun chunk metni bu projeksiyonda dönmemeli",
                    vector=[1.0, 0.0, 0.0, 0.0],
                )
            ]
        )

        monkeypatch.setattr(client, "scroll", _spy_scroll)
        monkeypatch.setattr(client, "facet", _failing_facet)

        docs = await service.list_documents(tenant_id="tenant_meta")
        assert len(docs) == 1
        assert docs[0].document_id == "doc-meta-1"
        assert scroll_with_payload_calls[0] == [
            "document_id",
            "filename",
            "file_type",
            "page_number",
        ]

        assert await service.list_tenants() == ["tenant_meta"]
        assert scroll_with_payload_calls[-1] == ["tenant_id"]
    finally:
        await service.close()
