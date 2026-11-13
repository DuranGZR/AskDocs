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

    async def read(self, size: int = -1) -> bytes:
        ...


class BaseStorage(ABC):
    """Dosya depolama katmanı için soyut arayüz (Local, S3/MinIO vb.)."""

    @abstractmethod
    async def save(self, *, file: AsyncReadableFile, filename: str | None) -> StoredFile:
        """Gelen dosya akışını doğrular, depolama alanına yazar ve meta verisini döner."""

    @abstractmethod
    async def delete(self, file_path: Path) -> None:
        """Verilen yoldaki dosyayı depolamadan güvenli şekilde siler."""