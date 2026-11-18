from pathlib import Path
from typing import Any

import pytest

import text_processor
from src.services.pdf_parser import (
    chunk_text as py_chunk_text,
)
from src.services.pdf_parser import (
    clean_text as py_clean_text,
)
from src.services.pdf_parser import (
    iter_document_pages,
)


def test_text_processor_bridge_metadata_and_health() -> None:
    """Native Rust köprüsü sürüm bilgisi ve sağlık kontrolünü doğru döndürmelidir."""
    assert text_processor.__version__ == "0.1.0"
    assert text_processor.version() == "0.1.0"
    assert text_processor.health_check() is True


@pytest.mark.parametrize(
    "sample",
    [
        "",
        "CoreRAG-Engine",
        "Şirket içi güvenlik: ğüşıöçĞÜŞİÖÇ",
        "Birinci satır.\nİkinci satır.\tÜçüncü bölüm.",
        "🚀📄 AskDocs Türkçe + Emoji doğrulama 123!",
    ],
)
def test_text_processor_count_chars_matches_python_len(sample: str) -> None:
    """Rust UTF-8 char sayımı ASCII, Türkçe ve emoji metinlerde Python len() ile eşleşmelidir."""
    assert text_processor.count_chars(sample) == len(sample)


def test_text_processor_rejects_non_string_input() -> None:
    """PyO3 imza doğrulaması string dışı argümanda TypeError fırlatmalıdır."""
    invalid_input: Any = 123
    with pytest.raises(TypeError):
        text_processor.count_chars(invalid_input)


@pytest.mark.parametrize(
    "raw_text",
    [
        "",
        "   \r\n\t   \n\n  ",
        (
            "<script>alert(1)</script><p>Merhaba   dün-\nyası!</p>\x00\x07\r\n\r\n\r\n"
            "İkinci   satır\t\tmetni."
        ),
        "<STYLE type='text/css'>body { margin: 0; }</STYLE>\n\nBaşlık\rSatır 2",
        "2 < 5 and <script>evil()</script> <scripts>korunur</scripts> <> son",
        "a-\nb-\nc ile foo-\nbar-\nbaz ve a--\nb birleşimi",
        "Türkçe karakterler: ğüşıöç ĞÜŞİÖÇ\u00a0\u00a0🚀\x0b\x0c\x1f\x7fson.",
        "Paragraf 1\n   \n\t\n\nParagraf 2\r\n\r\n\r\nParagraf 3",
    ],
)
def test_clean_text_exact_parity_with_python_reference(raw_text: str) -> None:
    """Rust clean_text çıktısı tüm sınır vakalarında Python referansıyla birebir eşleşmelidir."""
    assert text_processor.clean_text(raw_text) == py_clean_text(raw_text)


@pytest.mark.parametrize(
    ("chunk_size", "chunk_overlap"),
    [
        (140, 30),
        (500, 75),
        (1200, 150),
        (80, 0),
        (35, 10),
    ],
)
def test_chunk_text_exact_parity_with_python_reference(
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    """Rust chunk_text çıktısı Türkçe, emoji ve uzun kelimelerde Python ile birebir eşleşmelidir."""
    corpus = (
        "<p>AskDocs kurumsal gizlilik ve güvenlik poli-\ntikası rehberi.</p>\r\n\r\n"
        + " ".join(
            f"Madde {i}: Şirket içi çalışanlar ğüşıöçĞÜŞİÖÇ verilerini şifreli 🚀 saklamalıdır."
            for i in range(1, 25)
        )
        + "\n\n"
        + "A" * 95
        + " Son cümle burada biter!"
    )

    assert text_processor.chunk_text(
        corpus,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    ) == py_chunk_text(
        corpus,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def test_chunk_text_parity_on_real_pdf_pages_when_available() -> None:
    """Yerel örnek PDF dosyaları mevcutsa gerçek sayfa metinlerinde de birebir uyum doğrulanır."""
    sample_pdfs = [
        Path("data/sirket_el_kitabi_tr.pdf"),
        Path("data/bitcoin_whitepaper.pdf"),
    ]
    for pdf_path in sample_pdfs:
        if not pdf_path.is_file():
            continue
        for page in iter_document_pages(pdf_path, file_type="pdf"):
            assert text_processor.clean_text(page.text) == py_clean_text(page.text)
            assert text_processor.chunk_text(
                page.text,
                chunk_size=1200,
                chunk_overlap=150,
            ) == py_chunk_text(
                page.text,
                chunk_size=1200,
                chunk_overlap=150,
            )


def test_chunk_text_invalid_arguments_raise_value_error() -> None:
    """Geçersiz chunk_size veya chunk_overlap değerleri ValueError fırlatmalıdır."""
    with pytest.raises(ValueError, match="chunk_size"):
        text_processor.chunk_text("metin", chunk_size=0, chunk_overlap=0)
    with pytest.raises(ValueError, match="chunk_size"):
        text_processor.chunk_text("metin", chunk_size=-10, chunk_overlap=0)
    with pytest.raises(ValueError, match="chunk_overlap"):
        text_processor.chunk_text("metin", chunk_size=100, chunk_overlap=-1)
    with pytest.raises(ValueError, match="chunk_overlap"):
        text_processor.chunk_text("metin", chunk_size=100, chunk_overlap=100)
