from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.core.config import Settings
from src.core.exceptions import BadRequestError
from src.services.storage_service import LocalStorageService


class _FakeReadableStream:
    """FastAPI bağımlılığı olmadan AsyncReadableFile sözleşmesini simüle eder."""

    def __init__(self, chunks: list[bytes], *, fail_after_first: bool = False) -> None:
        self._chunks = list(chunks)
        self._fail_after_first = fail_after_first
        self._reads = 0

    async def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._fail_after_first and self._reads > 1:
            raise OSError("simulated disk read failure")
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_settings_normalizes_case_and_computes_bytes() -> None:
    """Ortam değişkenleri büyük/küçük harf normalize edilmeli ve bayt sınırı hesaplanmalıdır."""
    raw_values: dict[str, Any] = {
        "APP_ENV": "  Production ",
        "LOG_LEVEL": "warning",
        "LOG_FORMAT": "JSON",
        "DEBUG": False,
        "API_KEY": "prod-secret-key-999",
        "MAX_UPLOAD_SIZE_MB": 2,
    }
    settings = Settings(_env_file=None, **raw_values)

    assert settings.APP_ENV == "production"
    assert settings.LOG_LEVEL == "WARNING"
    assert settings.LOG_FORMAT == "json"
    assert settings.max_upload_size_bytes == 2 * 1024 * 1024


@pytest.mark.parametrize(
    "overrides",
    [
        {"APP_ENV": "production", "API_KEY": "change-me-in-local-env", "DEBUG": False},
        {"APP_ENV": "production", "API_KEY": "strong-prod-key-123", "DEBUG": True},
        {"APP_ENV": "development", "API_KEY": "short"},
        {"APP_ENV": "development", "API_KEY": "        "},
    ],
)
def test_settings_rejects_unsafe_or_invalid_values(overrides: dict[str, Any]) -> None:
    """Üretimde güvensiz varsayılanlar veya kısa API anahtarı reddedilmelidir."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


async def test_local_storage_cleans_up_when_stream_fails_mid_write(tmp_path: Path) -> None:
    """Akış ortasında okuma hatası oluşursa kısmi dosya diskte bırakılmamalıdır."""
    storage = LocalStorageService(base_dir=tmp_path, max_size_bytes=1024)
    broken_stream = _FakeReadableStream([b"%PDF-1.4"], fail_after_first=True)

    with pytest.raises(OSError, match="simulated disk read failure"):
        await storage.save(file=broken_stream, filename="report.pdf")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_name", [None, "", "   "])
async def test_local_storage_rejects_missing_filename(
    tmp_path: Path,
    bad_name: str | None,
) -> None:
    """Dosya adı eksik veya boşsa BadRequestError yükseltilmelidir."""
    storage = LocalStorageService(base_dir=tmp_path, max_size_bytes=1024)
    stream = _FakeReadableStream([b"hello"])

    with pytest.raises(BadRequestError, match="Filename is required"):
        await storage.save(file=stream, filename=bad_name)


async def test_local_storage_rejects_empty_stream_before_creating_dir(tmp_path: Path) -> None:
    """Boş akış reddedildiğinde hedef klasör veya 0-baytlık dosya oluşturulmamalıdır."""
    target_dir = tmp_path / "never_created"
    storage = LocalStorageService(base_dir=target_dir, max_size_bytes=1024)
    empty_stream = _FakeReadableStream([])

    with pytest.raises(BadRequestError, match="Uploaded file is empty"):
        await storage.save(file=empty_stream, filename="empty.pdf")

    assert not target_dir.exists()


async def test_local_storage_delete_missing_file_is_safe(tmp_path: Path) -> None:
    """Mevcut olmayan dosya silinmeye çalışıldığında hata fırlatılmamalıdır."""
    storage = LocalStorageService(base_dir=tmp_path, max_size_bytes=1024)
    await storage.delete(tmp_path / "already-deleted.txt")
