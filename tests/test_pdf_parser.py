from pathlib import Path

import pymupdf
import pytest

from src.services.pdf_parser import (
    DocumentParseError,
    chunk_text,
    clean_text,
    parse_and_chunk_document,
)


def _create_sample_pdf(path: Path, pages: list[str]) -> None:
    """Testler için gerçek çok sayfalı PDF belgesi üretir."""
    with pymupdf.open() as doc:
        rect = pymupdf.Rect(50, 50, 540, 750)
        for text in pages:
            page = doc.new_page()
            page.insert_textbox(rect, text)
        doc.save(path)


def test_clean_text_strips_html_control_chars_and_normalizes_whitespace() -> None:
    """clean_text HTML/script etiketlerini, kontrol karakterlerini ve tire bölünmesini temizler."""
    raw = (
        "<script>alert(1)</script><p>Merhaba   dün-\nyası!</p>\x00\x07\r\n\r\n\r\n"
        "İkinci   satır\t\tmetni."
    )

    cleaned = clean_text(raw)

    assert cleaned == "Merhaba dünyası!\n\nİkinci satır metni."


def test_chunk_text_splits_with_word_boundaries_and_overlap() -> None:
    """chunk_text boyut sınırını aşmadan kelime bütünlüğünü ve örtüşmeyi korumalıdır."""
    sentences = [f"Cümle numarası {i} sistem dokümantasyonu için üretildi." for i in range(1, 15)]
    text = " ".join(sentences)

    chunks = chunk_text(text, chunk_size=140, chunk_overlap=30)

    assert len(chunks) > 2
    assert all(0 < len(chunk) <= 140 for chunk in chunks)
    # Ardışık parçalar arasında örtüşen ortak kelimeler bulunmalı
    first_tail_words = set(chunks[0].split()[-4:])
    second_head_words = set(chunks[1].split()[:6])
    assert first_tail_words & second_head_words

    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text(text, chunk_size=100, chunk_overlap=100)


def test_parse_and_chunk_pdf_reads_page_by_page_and_assigns_metadata(tmp_path: Path) -> None:
    """Çok sayfalı PDF sayfa sayfa okunup tenant ve sayfa metadata'sıyla parçalanmalıdır."""
    pdf_path = tmp_path / "contract.pdf"
    page_1 = " ".join(f"Birinci sayfa maddesi {i}." for i in range(25))
    page_2 = " ".join(f"İkinci sayfa hükmü {i}." for i in range(10))
    _create_sample_pdf(pdf_path, [page_1, page_2])

    parsed = parse_and_chunk_document(
        file_path=pdf_path,
        document_id="doc-pdf-1",
        tenant_id="acme_corp",
        file_type="pdf",
        chunk_size=160,
        chunk_overlap=25,
    )

    assert parsed.page_count == 2
    assert len(parsed.chunks) >= 3
    assert parsed.chunks[0].page_number == 1
    assert parsed.chunks[-1].page_number == 2
    assert [chunk.chunk_index for chunk in parsed.chunks] == list(range(len(parsed.chunks)))
    assert all(chunk.document_id == "doc-pdf-1" for chunk in parsed.chunks)
    assert all(chunk.tenant_id == "acme_corp" for chunk in parsed.chunks)
    assert all(chunk.file_type == "pdf" for chunk in parsed.chunks)


def test_parse_and_chunk_corrupt_pdf_raises_document_parse_error(tmp_path: Path) -> None:
    """Bozuk PDF dosyası retry edilmeyen DocumentParseError hatası vermelidir."""
    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"%PDF-1.4 broken-corrupted-binary-stream")

    with pytest.raises(DocumentParseError):
        parse_and_chunk_document(
            file_path=corrupt_pdf,
            document_id="doc-corrupt",
            tenant_id="acme_corp",
            file_type="pdf",
        )
