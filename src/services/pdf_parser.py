import re
from collections.abc import Iterator
from pathlib import Path

import pymupdf

from src.services.base import DocumentChunk, PageContent, ParsedDocument

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HYPHEN_WRAP_RE = re.compile(r"(\w)-\n(\w)")
_HORIZONTAL_WS_RE = re.compile(r"[^\S\n]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_TXT_PAGE_CHAR_LIMIT = 65_536


class DocumentParseError(ValueError):
    """Bozuk, okunamayan veya geçersiz doküman içeriklerinde fırlatılır (retry edilmez)."""


class TransientProcessingError(RuntimeError):
    """Geçici I/O veya dış servis kesintilerinde Celery retry tetiklemek için fırlatılır."""


def clean_text(raw_text: str) -> str:
    """HTML kalıntılarını, kontrol karakterlerini ve gereksiz boşlukları temizler."""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _CONTROL_CHAR_RE.sub("", text)
    text = _HYPHEN_WRAP_RE.sub(r"\1\2", text)
    text = _HORIZONTAL_WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    normalized = "\n".join(lines)
    return _MULTI_NEWLINE_RE.sub("\n\n", normalized).strip()


def _find_split_end(text: str, start: int, max_end: int) -> int:
    """Mümkün olduğunca cümle veya kelime sınırından temiz bitiş indeksi seçer."""
    midpoint = start + (max_end - start) // 2
    for separator in (". ", "! ", "? ", "\n"):
        pos = text.rfind(separator, midpoint, max_end)
        if pos != -1:
            return pos + (0 if separator == "\n" else 1)

    space_pos = text.rfind(" ", midpoint, max_end)
    if space_pos > start:
        return space_pos
    return max_end


def _find_overlap_start(text: str, start: int, end: int, chunk_overlap: int) -> int:
    """Bir sonraki parça başlangıcını kelime ortasına düşmeyecek şekilde hizalar."""
    if chunk_overlap == 0:
        return end

    candidate = max(start + 1, end - chunk_overlap)
    if 0 < candidate < len(text) and not text[candidate - 1].isspace():
        next_space = text.find(" ", candidate, end)
        if next_space != -1 and next_space + 1 < end:
            return next_space + 1
    return candidate


def chunk_text(
    text: str,
    *,
    chunk_size: int = 500,
    chunk_overlap: int = 75,
) -> list[str]:
    """Metni cümle/kelime sınırlarını koruyarak örtüşmeli parçalara ayırır."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")

    cleaned = clean_text(text)
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    chunks: list[str] = []
    start = 0
    total_len = len(cleaned)

    while start < total_len:
        max_end = min(start + chunk_size, total_len)
        end = total_len if max_end == total_len else _find_split_end(cleaned, start, max_end)

        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= total_len:
            break

        next_start = _find_overlap_start(cleaned, start, end, chunk_overlap)
        while next_start < total_len and cleaned[next_start].isspace():
            next_start += 1
        start = next_start if next_start > start else end

    return chunks


def _iter_txt_pages(file_path: Path, *, page_char_limit: int) -> Iterator[PageContent]:
    """Düz metin dosyalarını sabit boyutlu bloklarla belleği doldurmadan sayfa sayfa okur."""
    if page_char_limit <= 0:
        raise ValueError("page_char_limit must be greater than 0")

    buffer = ""
    page_number = 0
    read_size = min(page_char_limit, 16_384)
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                chunk = handle.read(read_size)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= page_char_limit:
                    split_at = buffer.rfind("\n", 0, page_char_limit)
                    if split_at <= 0:
                        split_at = buffer.rfind(" ", 0, page_char_limit)
                    if split_at <= 0:
                        split_at = page_char_limit
                    page_number += 1
                    yield PageContent(page_number=page_number, text=buffer[:split_at])
                    buffer = buffer[split_at:].lstrip("\r\n ")
    except OSError as exc:
        raise DocumentParseError(f"Failed to read text document: {file_path}") from exc

    if buffer or page_number == 0:
        page_number += 1
        yield PageContent(page_number=page_number, text=buffer)


def iter_document_pages(
    file_path: Path,
    *,
    file_type: str | None = None,
    txt_page_char_limit: int | None = None,
) -> Iterator[PageContent]:
    """Dokümanı belleğe komple yüklemeden sayfa sayfa (stream) okur."""
    if not file_path.is_file():
        raise DocumentParseError(f"Document file not found: {file_path}")

    resolved_type = (file_type or file_path.suffix).lstrip(".").lower()
    if resolved_type == "pdf":
        try:
            doc = pymupdf.open(file_path)
        except Exception as exc:
            raise DocumentParseError("Failed to open PDF document") from exc

        try:
            if len(doc) == 0:
                raise DocumentParseError("PDF document has no pages")
            for page_index in range(len(doc)):
                page = doc.load_page(page_index)
                yield PageContent(
                    page_number=page_index + 1,
                    text=str(page.get_text("text")),
                )
        finally:
            doc.close()
        return

    if resolved_type == "txt":
        resolved_limit = (
            _TXT_PAGE_CHAR_LIMIT if txt_page_char_limit is None else txt_page_char_limit
        )
        yield from _iter_txt_pages(file_path, page_char_limit=resolved_limit)
        return

    raise DocumentParseError(f"Unsupported document type: {resolved_type}")


def parse_and_chunk_document(
    *,
    file_path: Path,
    document_id: str,
    tenant_id: str,
    file_type: str,
    chunk_size: int = 500,
    chunk_overlap: int = 75,
    filename: str = "",
) -> ParsedDocument:
    """Dokümanı sayfa sayfa okuyup temizleyerek metadata'lı chunk listesi üretir."""
    chunks: list[DocumentChunk] = []
    page_count = 0
    chunk_index = 0

    for page in iter_document_pages(file_path, file_type=file_type):
        page_count += 1
        for piece in chunk_text(
            page.text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        ):
            chunks.append(
                DocumentChunk(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    page_number=page.page_number,
                    chunk_index=chunk_index,
                    text=piece,
                    file_type=file_type,
                    filename=filename,
                )
            )
            chunk_index += 1

    return ParsedDocument(
        document_id=document_id,
        tenant_id=tenant_id,
        file_type=file_type,
        page_count=page_count,
        chunks=chunks,
    )
