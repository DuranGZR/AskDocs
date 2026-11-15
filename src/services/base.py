from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Başarıyla depolanan bir dosyanın servis katmanı çıktı özeti."""

    document_id: str
    original_filename: str
    file_type: str
    file_path: Path
    size_bytes: int


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
