import asyncio
from pathlib import Path, PurePath
from uuid import uuid4

import aiofiles

from src.core.exceptions import (
    BadRequestError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from src.services.base import AsyncReadableFile, BaseStorage, StoredFile

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".txt"})
DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB
PDF_MAGIC_BYTES = b"%PDF-"


class LocalStorageService(BaseStorage):
    """Paylaşımlı yerel dizine RAM dostu, parça parça (stream) dosya kaydı yapar."""

    def __init__(
        self,
        base_dir: Path,
        max_size_bytes: int,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._base_dir = base_dir
        self._max_size_bytes = max_size_bytes
        self._chunk_size = chunk_size

    async def save(self, *, file: AsyncReadableFile, filename: str | None) -> StoredFile:
        """Dosya adını/uzantısını doğrular ve akış halinde diske yazar."""
        safe_original_name = PurePath(filename).name.strip() if filename else ""
        if not safe_original_name:
            raise BadRequestError("Filename is required")

        extension = PurePath(safe_original_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise UnsupportedMediaTypeError(
                f"Unsupported file type '{extension or 'none'}'. Allowed types: .pdf, .txt"
            )

        first_chunk = await file.read(self._chunk_size)
        if not first_chunk:
            raise BadRequestError("Uploaded file is empty")

        while extension == ".pdf" and len(first_chunk) < len(PDF_MAGIC_BYTES):
            more = await file.read(len(PDF_MAGIC_BYTES) - len(first_chunk))
            if not more:
                break
            first_chunk += more

        if len(first_chunk) > self._max_size_bytes:
            raise PayloadTooLargeError(
                f"File exceeds maximum allowed size of {self._max_size_bytes} bytes"
            )
        self._validate_content_signature(extension=extension, first_chunk=first_chunk)

        await asyncio.to_thread(self._base_dir.mkdir, parents=True, exist_ok=True)
        document_id = str(uuid4())
        target_path = self._base_dir / f"{document_id}{extension}"

        size_bytes = len(first_chunk)
        try:
            async with aiofiles.open(target_path, mode="wb") as out_file:
                await out_file.write(first_chunk)
                while chunk := await file.read(self._chunk_size):
                    size_bytes += len(chunk)
                    if size_bytes > self._max_size_bytes:
                        raise PayloadTooLargeError(
                            f"File exceeds maximum allowed size of {self._max_size_bytes} bytes"
                        )
                    await out_file.write(chunk)
        except Exception:
            await self.delete(target_path)
            raise

        return StoredFile(
            document_id=document_id,
            original_filename=safe_original_name,
            file_type=extension.lstrip("."),
            file_path=target_path,
            size_bytes=size_bytes,
        )

    async def delete(self, file_path: Path) -> None:
        """Diskten dosyayı event loop'u bloklamadan güvenli şekilde siler."""
        await asyncio.to_thread(file_path.unlink, missing_ok=True)

    @staticmethod
    def _validate_content_signature(extension: str, first_chunk: bytes) -> None:
        """Dosya uzantısı ile içerik imzasının (Magic Bytes) uyumlu olduğunu doğrular."""
        if extension == ".pdf" and not first_chunk.startswith(PDF_MAGIC_BYTES):
            raise UnsupportedMediaTypeError(
                "Invalid PDF file content: missing '%PDF-' magic bytes signature"
            )
        if extension == ".txt" and b"\x00" in first_chunk:
            raise UnsupportedMediaTypeError("Invalid text file content: binary null bytes detected")
