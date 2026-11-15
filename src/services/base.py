from abc import ABC, abstractmethod
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


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Sayfa sayfa okunup parçalanmış dokümanın servis çıktısı."""

    document_id: str
    tenant_id: str
    file_type: str
    page_count: int
    chunks: list[DocumentChunk]


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
    ) -> str:
        """Görevi kuyruğa ekler ve üretilen task_id değerini döner."""
        ...

    async def get_task_status(self, *, task_id: str) -> TaskStatusSnapshot:
        """Verilen task_id için güncel görev durumunu arka uçtan sorgular."""
        ...
