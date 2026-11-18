from pathlib import Path
from typing import Any

import pymupdf
import pytest

import text_processor
from scripts.benchmark_text_processor import run_benchmark
from src.services import pdf_parser
from src.services.pdf_parser import (
    ACTIVE_TEXT_PROCESSOR_BACKEND,
    chunk_text,
    clean_text,
    iter_document_pages,
    parse_and_chunk_document,
    py_chunk_text,
    py_clean_text,
)
from src.services.qdrant_service import build_chunk_point_id


def test_text_processor_bridge_metadata_and_health() -> None:
    """Native Rust köprüsü sürüm bilgisi, aktif backend ve sağlık kontrolünü doğru döndürmelidir."""
    assert text_processor.__version__ == "0.1.0"
    assert text_processor.version() == "0.1.0"
    assert text_processor.health_check() is True
    assert ACTIVE_TEXT_PROCESSOR_BACKEND == "rust"


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


def test_parse_and_chunk_document_and_point_ids_parity_on_real_pdfs(tmp_path: Path) -> None:
    """Rust aktifken PDF chunk ve UUID5 kimlikleri Python referansıyla birebir aynı olmalıdır."""
    generated_pdf = tmp_path / "ci_parity_sample.pdf"
    with pymupdf.open() as doc:
        rect = pymupdf.Rect(50, 50, 540, 750)
        for page_idx in range(2):
            page = doc.new_page()
            page_text = " ".join(
                (
                    f"Sayfa {page_idx + 1} cümle {i}: "
                    "Kurumsal Türkçe şif-\nreleme ve 🚀 veri güvenliği."
                )
                for i in range(1, 20)
            )
            page.insert_textbox(rect, page_text)
        doc.save(generated_pdf)

    sample_pdfs = [
        generated_pdf,
        Path("data/sirket_el_kitabi_tr.pdf"),
        Path("data/bitcoin_whitepaper.pdf"),
        Path("data/rag_paper_2005_11401.pdf"),
    ]
    for pdf_path in sample_pdfs:
        if not pdf_path.is_file():
            continue

        parsed = parse_and_chunk_document(
            file_path=pdf_path,
            document_id="doc-parity-check",
            tenant_id="test-user",
            file_type="pdf",
            chunk_size=1200,
            chunk_overlap=150,
            filename=pdf_path.name,
        )

        expected_pairs: list[tuple[int, str]] = []
        for page in iter_document_pages(pdf_path, file_type="pdf"):
            for piece in py_chunk_text(page.text, chunk_size=1200, chunk_overlap=150):
                expected_pairs.append((page.page_number, piece))

        actual_pairs = [(chunk.page_number, chunk.text) for chunk in parsed.chunks]
        assert actual_pairs == expected_pairs

        actual_point_ids = [
            build_chunk_point_id(chunk.document_id, chunk.chunk_index) for chunk in parsed.chunks
        ]
        expected_point_ids = [
            build_chunk_point_id("doc-parity-check", idx) for idx in range(len(expected_pairs))
        ]
        assert actual_point_ids == expected_point_ids


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


def test_pdf_parser_falls_back_to_python_when_native_module_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native modül yokken clean_text ve chunk_text güvenli Python fallback'ine dönmelidir."""
    monkeypatch.setattr(pdf_parser, "_rust_text_processor", None)
    sample = "<p>Merhaba   dün-\nyası!</p> " + " ".join(f"Madde {i}." for i in range(1, 20))

    assert clean_text(sample) == py_clean_text(sample)
    assert chunk_text(sample, chunk_size=80, chunk_overlap=20) == py_chunk_text(
        sample,
        chunk_size=80,
        chunk_overlap=20,
    )


def test_benchmark_runner_smoke_verifies_parity_and_metrics() -> None:
    """Microbenchmark koşucusu küçük örnekte parity ve hız/bellek metriklerini üretmelidir."""
    report = run_benchmark(
        target_size_mb=0.05,
        chunk_size=1200,
        chunk_overlap=150,
        iterations=1,
    )
    assert report.active_backend == "rust"
    assert report.clean_text.parity_verified is True
    assert report.chunk_text.parity_verified is True
    assert report.clean_text.speedup > 0.0
    assert report.chunk_text.speedup > 0.0
    assert report.chunk_text.output_count > 0
