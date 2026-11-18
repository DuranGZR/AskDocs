from typing import Any

import pytest

import text_processor


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
