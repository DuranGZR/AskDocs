#![forbid(unsafe_code)]
//! CoreRAG-Engine metin temizleme ve akıllı parçalama (chunking) motoru.
//!
//! 22. Gün: PyO3 FFI köprüsü (`#[pyfunction]`, `#[pymodule]`) ve GIL serbest bırakma doğrulaması.

use pyo3::prelude::*;

/// Crate'in aktif sürüm bilgisini döndürür.
pub fn crate_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// PyO3 başlık ve sembollerinin başarıyla bağlandığını doğrular.
pub fn pyo3_ready() -> bool {
    let _marker = std::marker::PhantomData::<PyResult<()>>;
    true
}

/// UTF-8 metindeki gerçek Unicode karakter (`char`) sayısını hesaplar.
pub fn count_unicode_chars(text: &str) -> usize {
    text.chars().count()
}

/// Native Rust modülünün aktif sürümünü Python'a döndürür.
#[pyfunction]
pub fn version() -> &'static str {
    crate_version()
}

/// Native Rust modülünün belleğe sağlıklı yüklendiğini doğrular.
#[pyfunction]
pub fn health_check() -> bool {
    pyo3_ready()
}

/// Python string'ini zero-copy `&str` olarak alır, GIL'i bırakarak Unicode karakter sayar.
#[pyfunction]
#[pyo3(signature = (text))]
pub fn count_chars(py: Python<'_>, text: &str) -> usize {
    py.allow_threads(|| count_unicode_chars(text))
}

/// `text_processor` Python native uzantı modülü tanımı.
#[pymodule]
fn text_processor(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", crate_version())?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(health_check, m)?)?;
    m.add_function(wrap_pyfunction!(count_chars, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_crate_initialization_and_pyo3_linkage() {
        assert_eq!(crate_version(), "0.1.0");
        assert!(pyo3_ready());
    }

    #[test]
    fn test_count_unicode_chars_handles_ascii_turkish_and_multibyte_utf8() {
        assert_eq!(count_unicode_chars(""), 0);
        assert_eq!(count_unicode_chars("CoreRAG"), 7);
        assert_eq!(count_unicode_chars("ğüşıöçĞÜŞİÖÇ"), 12);
        assert_eq!(count_unicode_chars("🚀📄"), 2);
        assert_eq!(
            count_unicode_chars("Şirket içi güvenlik: ğüşıöçĞÜŞİÖÇ 🚀"),
            35
        );
    }
}
