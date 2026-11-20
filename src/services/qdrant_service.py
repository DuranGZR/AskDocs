import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from qdrant_client import AsyncQdrantClient, QdrantClient, models
from qdrant_client.conversions.common_types import PointId
from qdrant_client.http.exceptions import UnexpectedResponse

from src.core.circuit_breaker import CircuitBreaker, SyncCircuitBreaker
from src.core.exceptions import AppError, ServiceUnavailableError
from src.services.base import (
    BaseVectorDB,
    DocumentChunk,
    IndexedDocumentSummary,
    ScoredChunk,
    SyncBaseVectorDB,
    VectorPoint,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

_INDEXED_PAYLOAD_FIELDS: tuple[str, ...] = ("tenant_id", "document_id")
_LIST_DOCUMENT_PAYLOAD_FIELDS: list[str] = [
    "document_id",
    "filename",
    "file_type",
    "page_number",
]
_TENANT_FACET_LIMIT: int = 1000


@dataclass(slots=True)
class _DocumentAggregate:
    file_type: str
    filename: str = ""
    pages: set[int] = field(default_factory=set)
    chunk_count: int = 0


def _extract_collection_vector_size(collection_info: Any) -> int | None:
    """Qdrant koleksiyon bilgisinden tanımlı vektör boyutunu güvenli şekilde okur."""
    config = getattr(collection_info, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    size = getattr(vectors, "size", None)
    if isinstance(size, int) and not isinstance(size, bool):
        return size
    if isinstance(vectors, Mapping):
        default_vec = vectors.get("")
        mapped_size = getattr(default_vec, "size", None)
        if isinstance(mapped_size, int) and not isinstance(mapped_size, bool):
            return mapped_size
    return None


def build_chunk_point_id(document_id: str, chunk_index: int) -> str:
    """Aynı doküman ve chunk_index için her zaman aynı UUID5 nokta ID'sini üretir."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{document_id}:{chunk_index}"))


def _to_point_struct(point: VectorPoint) -> models.PointStruct:
    """Servis katmanı VectorPoint nesnesini Qdrant PointStruct formatına çevirir."""
    return models.PointStruct(
        id=point.point_id,
        vector=list(point.vector),
        payload={
            "document_id": point.chunk.document_id,
            "tenant_id": point.chunk.tenant_id,
            "page_number": point.chunk.page_number,
            "chunk_index": point.chunk.chunk_index,
            "text": point.chunk.text,
            "file_type": point.chunk.file_type,
            "filename": point.chunk.filename,
        },
    )


def _payload_to_scored_chunk(
    *,
    point_id: PointId,
    score: float,
    payload: Mapping[str, Any] | None,
) -> ScoredChunk:
    """Qdrant payload sözlüğünü servis katmanı ScoredChunk nesnesine dönüştürür."""
    data = payload or {}
    chunk = DocumentChunk(
        document_id=str(data.get("document_id", "")),
        tenant_id=str(data.get("tenant_id", "")),
        page_number=int(data.get("page_number", 1)),
        chunk_index=int(data.get("chunk_index", 0)),
        text=str(data.get("text", "")),
        file_type=str(data.get("file_type", "pdf")),
        filename=str(data.get("filename") or ""),
    )
    return ScoredChunk(
        point_id=str(point_id),
        score=float(score),
        chunk=chunk,
    )


def _compute_document_quotas(
    by_doc: Mapping[str, Sequence[ScoredChunk]],
    limit: int,
) -> dict[str, int]:
    """Belgeler arasında dengeli dağıtım yapar, kalan bütçeyi daha uzun belgelere aktarır."""
    quotas = {doc_key: 0 for doc_key in by_doc}
    remaining = limit

    while remaining > 0:
        eligible = [doc_key for doc_key, chunks in by_doc.items() if quotas[doc_key] < len(chunks)]
        if not eligible:
            break
        step, rem = divmod(remaining, len(eligible))
        if step == 0:
            for doc_key in eligible[:rem]:
                quotas[doc_key] += 1
            break
        progressed = 0
        for doc_key in eligible:
            capacity = len(by_doc[doc_key]) - quotas[doc_key]
            take = min(step, capacity)
            quotas[doc_key] += take
            progressed += take
        remaining -= progressed

    return quotas


def _to_scored_chunk(point: models.ScoredPoint) -> ScoredChunk:
    """Qdrant ScoredPoint sonucunu servis katmanı ScoredChunk nesnesine çevirir."""
    return _payload_to_scored_chunk(point_id=point.id, score=point.score, payload=point.payload)


def _build_tenant_filter(
    tenant_id: str,
    document_id: str | None = None,
    document_ids: Sequence[str] | None = None,
) -> models.Filter:
    """Multi-tenant izolasyonu ve opsiyonel doküman kapsam filtresi kurar."""
    conditions: list[models.Condition] = [
        models.FieldCondition(
            key="tenant_id",
            match=models.MatchValue(value=tenant_id),
        )
    ]
    if document_id is not None:
        conditions.append(
            models.FieldCondition(
                key="document_id",
                match=models.MatchValue(value=document_id),
            )
        )
    elif document_ids:
        clean_ids = [doc_id.strip() for doc_id in document_ids if doc_id.strip()]
        if clean_ids:
            conditions.append(
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchAny(any=clean_ids),
                )
            )
    return models.Filter(must=conditions)


class AsyncQdrantService(BaseVectorDB):
    """FastAPI katmanı için AsyncQdrantClient tabanlı asenkron vektör veritabanı servisi."""

    def __init__(
        self,
        *,
        collection_name: str,
        vector_size: int,
        url: str | None = None,
        client: AsyncQdrantClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        if client is None and not url:
            raise ValueError("Either 'url' or 'client' must be provided")
        self._collection_name = collection_name
        self._vector_size = vector_size
        self._client = client if client is not None else AsyncQdrantClient(url=url)
        self._circuit_breaker = circuit_breaker

    async def _run_guarded(self, op: Callable[[], Awaitable[_T]]) -> _T:
        """Qdrant çağrısını devre kesici koruması ve standart 503 hata eşlemesiyle yürütür."""

        async def _invoke() -> _T:
            try:
                return await op()
            except AppError:
                raise
            except Exception as exc:
                logger.error("Async Qdrant operation failed: %s", type(exc).__name__)
                raise ServiceUnavailableError("Vector database is currently unavailable.") from exc

        if self._circuit_breaker is not None:
            return await self._circuit_breaker.call(_invoke)
        return await op()

    async def ensure_collection(self) -> None:
        """Koleksiyon yoksa veya boyutu uyumsuzsa oluşturur; payload indekslerini tanımlar."""
        exists = await self._client.collection_exists(self._collection_name)
        if exists:
            try:
                info = await self._client.get_collection(self._collection_name)
            except AttributeError:
                info = None
            existing_size = _extract_collection_vector_size(info)
            if existing_size is not None and existing_size != self._vector_size:
                logger.warning(
                    "Qdrant collection '%s' vector_size mismatch "
                    "(existing=%d, configured=%d); recreating collection",
                    self._collection_name,
                    existing_size,
                    self._vector_size,
                )
                await self._client.delete_collection(self._collection_name)
                exists = False

        if not exists:
            await self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config=models.VectorParams(
                    size=self._vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info(
                "Created Qdrant collection '%s' (vector_size=%d, distance=Cosine)",
                self._collection_name,
                self._vector_size,
            )

        for field_name in _INDEXED_PAYLOAD_FIELDS:
            await self._client.create_payload_index(
                collection_name=self._collection_name,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
        """Vektör noktalarını idempotent olarak Qdrant'a yazar."""
        if not points:
            return 0
        qdrant_points = [_to_point_struct(point) for point in points]

        async def _op() -> int:
            await self._client.upsert(
                collection_name=self._collection_name,
                points=qdrant_points,
                wait=True,
            )
            return len(qdrant_points)

        return await self._run_guarded(_op)

    async def search(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        top_k: int = 5,
        score_threshold: float = 0.5,
        document_ids: Sequence[str] | None = None,
    ) -> list[ScoredChunk]:
        """Sorgu vektörünü tenant_id (ve opsiyonel document_ids) filtresiyle arar."""

        async def _op() -> list[ScoredChunk]:
            response = await self._client.query_points(
                collection_name=self._collection_name,
                query=list(query_vector),
                query_filter=_build_tenant_filter(
                    tenant_id=tenant_id,
                    document_ids=document_ids,
                ),
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )
            return [_to_scored_chunk(point) for point in response.points]

        return await self._run_guarded(_op)

    async def search_scoped(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        top_k: int = 5,
        score_threshold: float = 0.5,
        document_ids: Sequence[str] | None = None,
    ) -> list[ScoredChunk]:
        """Opsiyonel document_ids filtresiyle vektör araması yapar."""
        return await self.search(
            query_vector=query_vector,
            tenant_id=tenant_id,
            top_k=top_k,
            score_threshold=score_threshold,
            document_ids=document_ids,
        )

    async def sample_document_chunks(
        self,
        *,
        tenant_id: str,
        document_ids: Sequence[str] | None = None,
        limit: int = 12,
    ) -> list[ScoredChunk]:
        """Dokümanların başından sonuna tüm sayfalardan dengeli chunk örneklemi döndürür."""
        if limit <= 0:
            return []

        async def _op() -> list[ScoredChunk]:
            scroll_filter = _build_tenant_filter(tenant_id=tenant_id, document_ids=document_ids)
            collected: list[ScoredChunk] = []
            offset: PointId | None = None
            max_scan = 2000

            while len(collected) < max_scan:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection_name,
                    scroll_filter=scroll_filter,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for record in records:
                    scored_item = _payload_to_scored_chunk(
                        point_id=record.id,
                        score=0.8,
                        payload=record.payload,
                    )
                    if scored_item.chunk.document_id and scored_item.chunk.text.strip():
                        collected.append(scored_item)
                if next_offset is None or not records:
                    break
                offset = next_offset

            if not collected:
                return []

            collected.sort(
                key=lambda item: (
                    item.chunk.document_id,
                    item.chunk.page_number,
                    item.chunk.chunk_index,
                )
            )
            by_doc: dict[str, list[ScoredChunk]] = {}
            for item in collected:
                by_doc.setdefault(item.chunk.document_id, []).append(item)

            quotas = _compute_document_quotas(by_doc, limit)
            sampled: list[ScoredChunk] = []
            for doc_key, doc_chunks in by_doc.items():
                quota = quotas[doc_key]
                if quota <= 0:
                    continue
                if len(doc_chunks) <= quota:
                    sampled.extend(doc_chunks)
                elif quota == 1:
                    sampled.append(doc_chunks[0])
                else:
                    indices = sorted(
                        {round(step * (len(doc_chunks) - 1) / (quota - 1)) for step in range(quota)}
                    )
                    sampled.extend(doc_chunks[pos] for pos in indices)

            return sampled[:limit]

        return await self._run_guarded(_op)

    async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        """Belirtilen tenant_id ve document_id'ye ait tüm chunk vektörlerini siler."""

        async def _op() -> None:
            await self._client.delete(
                collection_name=self._collection_name,
                points_selector=models.FilterSelector(
                    filter=_build_tenant_filter(tenant_id=tenant_id, document_id=document_id),
                ),
                wait=True,
            )

        await self._run_guarded(_op)

    async def list_documents(self, *, tenant_id: str) -> list[IndexedDocumentSummary]:
        """Belirtilen tenant_id altındaki dokümanları sayfalayarak tarar ve özetler."""

        async def _op() -> list[IndexedDocumentSummary]:
            grouped: dict[str, _DocumentAggregate] = {}
            offset: PointId | None = None
            tenant_filter = _build_tenant_filter(tenant_id=tenant_id)

            while True:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection_name,
                    scroll_filter=tenant_filter,
                    limit=256,
                    offset=offset,
                    with_payload=_LIST_DOCUMENT_PAYLOAD_FIELDS,
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    doc_id = str(payload.get("document_id") or "")
                    if not doc_id:
                        continue
                    file_type = str(payload.get("file_type") or "pdf")
                    filename = str(payload.get("filename") or "").strip()
                    page_number = int(payload.get("page_number") or 1)

                    entry = grouped.get(doc_id)
                    if entry is None:
                        entry = _DocumentAggregate(file_type=file_type, filename=filename)
                        grouped[doc_id] = entry
                    elif not entry.filename and filename:
                        entry.filename = filename

                    entry.chunk_count += 1
                    entry.pages.add(page_number)

                if next_offset is None:
                    break
                offset = next_offset

            summaries = [
                IndexedDocumentSummary(
                    document_id=doc_id,
                    tenant_id=tenant_id,
                    filename=entry.filename or f"{doc_id[:8]}.{entry.file_type}",
                    file_type=entry.file_type,
                    page_count=len(entry.pages) or 1,
                    chunk_count=entry.chunk_count,
                )
                for doc_id, entry in grouped.items()
            ]
            summaries.sort(key=lambda item: (item.filename.lower(), item.document_id))
            return summaries

        return await self._run_guarded(_op)

    async def list_tenants(self) -> list[str]:
        """Vektör veritabanında dokümanı bulunan benzersiz tenant_id listesini döndürür."""

        async def _scroll_tenants() -> list[str]:
            tenants: set[str] = set()
            offset: PointId | None = None

            while True:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection_name,
                    limit=256,
                    offset=offset,
                    with_payload=["tenant_id"],
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    tenant_id = str(payload.get("tenant_id") or "").strip()
                    if tenant_id:
                        tenants.add(tenant_id)

                if next_offset is None:
                    break
                offset = next_offset

            return sorted(tenants)

        async def _op() -> list[str]:
            try:
                facet_response = await self._client.facet(
                    collection_name=self._collection_name,
                    key="tenant_id",
                    limit=_TENANT_FACET_LIMIT,
                )
                if len(facet_response.hits) < _TENANT_FACET_LIMIT:
                    tenants = {
                        str(hit.value).strip()
                        for hit in facet_response.hits
                        if str(hit.value).strip()
                    }
                    return sorted(tenants)
            except (AttributeError, NotImplementedError, UnexpectedResponse) as exc:
                logger.debug(
                    "Qdrant facet fallback to scroll for list_tenants: %s",
                    type(exc).__name__,
                )

            return await _scroll_tenants()

        return await self._run_guarded(_op)

    async def ping(self, *, timeout_seconds: float = 2.0) -> bool:
        """Qdrant erişilebilirliğini devre kesiciden bağımsız olarak doğrular."""
        try:
            await asyncio.wait_for(self._client.get_collections(), timeout=timeout_seconds)
            return True
        except Exception as exc:
            logger.warning("Qdrant health ping failed: %s", type(exc).__name__)
            return False

    async def close(self) -> None:
        """Asenkron Qdrant istemci bağlantısını kapatır."""
        await self._client.close()


class SyncQdrantService(SyncBaseVectorDB):
    """Celery worker katmanı için QdrantClient tabanlı senkron vektör veritabanı servisi."""

    def __init__(
        self,
        *,
        collection_name: str,
        vector_size: int,
        url: str | None = None,
        client: QdrantClient | None = None,
        circuit_breaker: SyncCircuitBreaker | None = None,
    ) -> None:
        if client is None and not url:
            raise ValueError("Either 'url' or 'client' must be provided")
        self._collection_name = collection_name
        self._vector_size = vector_size
        self._client = client if client is not None else QdrantClient(url=url)
        self._circuit_breaker = circuit_breaker

    def _run_guarded(self, op: Callable[[], _T]) -> _T:
        """Senkron Qdrant çağrısını devre kesici koruması ve 503 eşlemesiyle yürütür."""

        def _invoke() -> _T:
            try:
                return op()
            except AppError:
                raise
            except Exception as exc:
                logger.error("Sync Qdrant operation failed: %s", type(exc).__name__)
                raise ServiceUnavailableError("Vector database is currently unavailable.") from exc

        if self._circuit_breaker is not None:
            return self._circuit_breaker.call(_invoke)
        return op()

    def ensure_collection(self) -> None:
        """Koleksiyon yoksa veya boyutu uyumsuzsa oluşturur; payload indekslerini tanımlar."""

        def _op() -> None:
            exists = self._client.collection_exists(self._collection_name)
            if exists:
                try:
                    info = self._client.get_collection(self._collection_name)
                except AttributeError:
                    info = None
                existing_size = _extract_collection_vector_size(info)
                if existing_size is not None and existing_size != self._vector_size:
                    logger.warning(
                        "Qdrant collection '%s' vector_size mismatch "
                        "(existing=%d, configured=%d); recreating collection",
                        self._collection_name,
                        existing_size,
                        self._vector_size,
                    )
                    self._client.delete_collection(self._collection_name)
                    exists = False

            if not exists:
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=models.VectorParams(
                        size=self._vector_size,
                        distance=models.Distance.COSINE,
                    ),
                )
                logger.info(
                    "Created Qdrant collection '%s' (vector_size=%d, distance=Cosine)",
                    self._collection_name,
                    self._vector_size,
                )

            for field_name in _INDEXED_PAYLOAD_FIELDS:
                self._client.create_payload_index(
                    collection_name=self._collection_name,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )

        self._run_guarded(_op)

    def upsert(self, *, points: Sequence[VectorPoint]) -> int:
        """Vektör noktalarını idempotent olarak Qdrant'a yazar."""
        if not points:
            return 0
        qdrant_points = [_to_point_struct(point) for point in points]

        def _op() -> int:
            self._client.upsert(
                collection_name=self._collection_name,
                points=qdrant_points,
                wait=True,
            )
            return len(qdrant_points)

        return self._run_guarded(_op)

    def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        """Belirtilen tenant_id ve document_id'ye ait tüm chunk vektörlerini siler."""

        def _op() -> None:
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=models.FilterSelector(
                    filter=_build_tenant_filter(tenant_id=tenant_id, document_id=document_id),
                ),
                wait=True,
            )

        self._run_guarded(_op)

    def close(self) -> None:
        """Senkron Qdrant istemci bağlantısını kapatır."""
        self._client.close()
