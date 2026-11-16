from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

TaskState = Literal["PENDING", "STARTED", "RETRY", "SUCCESS", "FAILURE"]


@dataclass(frozen=True, slots=True)
class TaskStatusSnapshot:
    """Kuyruk arka ucundan okunan görev durumunun servis katmanı özeti."""

    task_id: str
    status: TaskState
    result: dict[str, object] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Başarıyla depolanan bir dosyanın servis katmanı çıktı özeti."""

    document_id: str
    original_filename: str
    file_type: str
    file_path: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PageContent:
    """Dokümanın tek bir sayfasından çıkarılan ham metin."""

    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """Temizlenmiş ve örtüşmeli (overlapping) olarak parçalanmış metin birimi."""

    document_id: str
    tenant_id: str
    page_number: int
    chunk_index: int
    text: str
    file_type: str
    filename: str = ""


@dataclass(frozen=True, slots=True)
class IndexedDocumentSummary:
    """Vektör veritabanında indekslenmiş bir dokümanın özet meta verisi."""

    document_id: str
    tenant_id: str
    filename: str
    file_type: str
    page_count: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Sayfa sayfa okunup parçalanmış dokümanın servis çıktısı."""

    document_id: str
    tenant_id: str
    file_type: str
    page_count: int
    chunks: list[DocumentChunk]


@dataclass(frozen=True, slots=True)
class VectorPoint:
    """Vektör veritabanına yazılacak (upsert) tekil chunk ve embedding vektörü."""

    point_id: str
    vector: list[float]
    chunk: DocumentChunk


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """Vektör araması sonucunda benzerlik skoruyla dönen doküman parçası."""

    point_id: str
    score: float
    chunk: DocumentChunk


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """LLM sohbet istekleri için standart (OpenAI-uyumlu) mesaj birimi."""

    role: Literal["system", "user", "assistant"]
    content: str


class AsyncReadableFile(Protocol):
    """Parça parça (chunk) asenkron okuma yapılabilen dosya akışı sözleşmesi."""

    async def read(self, size: int = -1) -> bytes: ...


class BaseStorage(ABC):
    """Dosya depolama katmanı için soyut arayüz (Local, S3/MinIO vb.)."""

    @abstractmethod
    async def save(self, *, file: AsyncReadableFile, filename: str | None) -> StoredFile:
        """Gelen dosya akışını doğrular, depolama alanına yazar ve meta verisini döner."""

    @abstractmethod
    async def delete(self, file_path: Path) -> None:
        """Verilen yoldaki dosyayı depolamadan güvenli şekilde siler."""


class DocumentTaskDispatcher(Protocol):
    """Doküman işleme görevini arka plan kuyruğuna ileten sözleşme."""

    async def enqueue_document(
        self,
        *,
        document_id: str,
        file_path: Path,
        tenant_id: str,
        file_type: str,
        request_id: str,
        filename: str = "",
    ) -> str:
        """Görevi kuyruğa ekler ve üretilen task_id değerini döner."""
        ...

    async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
        """Verilen task_id için güncel görev durumunu arka uçtan sorgular."""
        ...


class BaseVectorDB(ABC):
    """Asenkron vektör veritabanı soyut arayüzü (FastAPI katmanı için)."""

    @abstractmethod
    async def ensure_collection(self) -> None:
        """Koleksiyonu ve zorunlu payload indekslerini (tenant_id, document_id) hazırlar."""

    @abstractmethod
    async def upsert(self, *, points: Sequence[VectorPoint]) -> int:
        """Vektör noktalarını idempotent olarak kaydeder ve yazılan kayıt sayısını döner."""

    @abstractmethod
    async def search(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        top_k: int = 5,
        score_threshold: float = 0.5,
    ) -> list[ScoredChunk]:
        """Sorgu vektörünü yalnızca ilgili tenant_id altında ve eşik skoruyla arar."""

    async def search_scoped(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        top_k: int = 5,
        score_threshold: float = 0.5,
        document_ids: Sequence[str] | None = None,
    ) -> list[ScoredChunk]:
        """Opsiyonel doküman listesi kapsamıyla vektör araması yapar."""
        del document_ids
        return await self.search(
            query_vector=query_vector,
            tenant_id=tenant_id,
            top_k=top_k,
            score_threshold=score_threshold,
        )

    @abstractmethod
    async def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        """Belirtilen tenant_id ve document_id'ye ait tüm vektör kayıtlarını siler."""

    async def list_documents(self, *, tenant_id: str) -> list[IndexedDocumentSummary]:
        """Belirtilen tenant_id altındaki indekslenmiş dokümanların özetini listeler."""
        return []

    async def list_tenants(self) -> list[str]:
        """Vektör veritabanında dokümanı bulunan tüm çalışma alanlarını listeler."""
        return ["test-user"]

    async def ping(self, *, timeout_seconds: float = 2.0) -> bool:
        """Vektör veritabanı bağlantısının canlılığını doğrular (devre kesiciden bağımsız)."""
        del timeout_seconds
        return True

    @abstractmethod
    async def close(self) -> None:
        """Açık ağ bağlantılarını ve istemci havuzunu güvenli şekilde kapatır."""


class SyncBaseVectorDB(ABC):
    """Senkron vektör veritabanı soyut arayüzü (Celery worker katmanı için)."""

    @abstractmethod
    def ensure_collection(self) -> None:
        """Koleksiyonu ve zorunlu payload indekslerini (tenant_id, document_id) hazırlar."""

    @abstractmethod
    def upsert(self, *, points: Sequence[VectorPoint]) -> int:
        """Vektör noktalarını idempotent olarak kaydeder ve yazılan kayıt sayısını döner."""

    @abstractmethod
    def delete_by_document(self, *, tenant_id: str, document_id: str) -> None:
        """Belirtilen tenant_id ve document_id'ye ait tüm vektör kayıtlarını siler."""

    @abstractmethod
    def close(self) -> None:
        """Açık ağ bağlantılarını güvenli şekilde kapatır."""


class BaseLLMClient(ABC):
    """Asenkron LLM ve Embedding istemcisi soyut arayüzü (FastAPI katmanı için)."""

    @abstractmethod
    async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        """Verilen metin listesini toplu halde embedding vektörlerine çevirir."""

    @abstractmethod
    async def generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Mesaj geçmişine göre modelden tam metin yanıt üretir."""

    @abstractmethod
    def stream_generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Mesaj geçmişine göre model yanıtını token bazlı akış (stream) olarak üretir."""

    @abstractmethod
    async def close(self) -> None:
        """Açık HTTP bağlantı havuzunu güvenli şekilde kapatır."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Hız sınırı kontrolünün izin ve kota bilgisini taşır."""

    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int
    retry_after_seconds: int


class BaseRateLimiter(ABC):
    """Dağıtık veya bellek içi hız sınırlayıcı soyut arayüzü."""

    @abstractmethod
    async def check_and_consume(
        self,
        *,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        """Belirtilen kapsam ve özne için kayan pencere kotasını atomik olarak kontrol eder."""

    async def ping(self, *, timeout_seconds: float = 2.0) -> bool:
        """Hız sınırlayıcı arka ucu (Redis) erişilebilirliğini kontrol eder."""
        del timeout_seconds
        return True

    @abstractmethod
    async def close(self) -> None:
        """Açık hız sınırlayıcı bağlantılarını güvenli şekilde kapatır."""


class BaseHealthChecker(ABC):
    """Sistem bağımlılıklarının (depolama, Redis, Qdrant) sağlık durumu arayüzü."""

    @abstractmethod
    async def check_dependencies(self) -> dict[str, str]:
        """Depolama, Redis ve Qdrant kontrollerini çalıştırıp durum sözlüğü döndürür."""


class SyncBaseLLMClient(ABC):
    """Senkron Embedding istemcisi soyut arayüzü (Celery worker katmanı için)."""

    @abstractmethod
    def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        """Verilen metin listesini toplu halde embedding vektörlerine çevirir."""

    @abstractmethod
    def close(self) -> None:
        """Açık HTTP bağlantılarını güvenli şekilde kapatır."""
