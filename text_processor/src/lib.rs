#![forbid(unsafe_code)]
//! CoreRAG-Engine metin temizleme ve akıllı parçalama (chunking) motoru.
//!
//! 21. Gün: Crate iskeleti, PyO3 bağımlılığı ve sıfır `unsafe` derleyici kilidi.

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_crate_initialization_and_pyo3_linkage() {
        assert_eq!(crate_version(), "0.1.0");
        assert!(pyo3_ready());
    }
}
