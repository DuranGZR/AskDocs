#![forbid(unsafe_code)]
//! CoreRAG-Engine metin temizleme ve akıllı parçalama (chunking) motoru.
//!
//! 23. Gün: Sıfır `unsafe`, harici `regex` crate'siz deterministik temizleme ve
//!     `&str` zero-copy dilimleme ile UTF-8 sınır güvenli chunking motoru.

use std::borrow::Cow;

use pyo3::exceptions::PyValueError;
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

#[inline]
fn is_word_char(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
}

#[inline]
fn is_control_byte(byte: u8) -> bool {
    matches!(byte, 0x00..=0x08 | 0x0b | 0x0c | 0x0e..=0x1f | 0x7f)
}

#[inline]
fn is_control_char(ch: char) -> bool {
    let code = ch as u32;
    code <= 0x7f && is_control_byte(code as u8)
}

/// Satır sonlarını (`\r\n` ve `\r`) yalnızca gerektiğinde kopyalayarak `\n` yapısına çevirir.
fn normalize_line_endings(raw_text: &str) -> Cow<'_, str> {
    if !raw_text.as_bytes().contains(&b'\r') {
        return Cow::Borrowed(raw_text);
    }

    let mut out = String::with_capacity(raw_text.len());
    let bytes = raw_text.as_bytes();
    let mut i = 0;
    let mut last_copied = 0;

    while i < bytes.len() {
        if bytes[i] == b'\r' {
            out.push_str(&raw_text[last_copied..i]);
            out.push('\n');
            if i + 1 < bytes.len() && bytes[i + 1] == b'\n' {
                i += 2;
            } else {
                i += 1;
            }
            last_copied = i;
        } else {
            i += 1;
        }
    }
    out.push_str(&raw_text[last_copied..]);
    Cow::Owned(out)
}

/// Büyük/küçük harf duyarsız ASCII öneki eşleştirir (yalnızca ASCII byte karşılaştırması).
#[inline]
fn starts_with_ignore_ascii_case(haystack: &str, needle: &str) -> bool {
    haystack
        .get(..needle.len())
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(needle))
}

/// Büyük/küçük harf duyarsız ASCII alt dizgisinin byte başlangıcını döndürür.
fn find_ignore_ascii_case(haystack: &str, needle: &str) -> Option<usize> {
    let n_bytes = needle.as_bytes();
    let h_bytes = haystack.as_bytes();
    if n_bytes.is_empty() || h_bytes.len() < n_bytes.len() {
        return None;
    }

    // `needle` tamamen ASCII olduğu için yalnızca `<` (`0x3c`) ile başlayan pozisyonları taramak güvenlidir
    let first_lower = n_bytes[0].to_ascii_lowercase();
    let first_upper = n_bytes[0].to_ascii_uppercase();

    for idx in 0..=h_bytes.len() - n_bytes.len() {
        let b = h_bytes[idx];
        if (b == first_lower || b == first_upper)
            && h_bytes[idx..idx + n_bytes.len()].eq_ignore_ascii_case(n_bytes)
            && haystack.is_char_boundary(idx)
        {
            return Some(idx);
        }
    }
    None
}

/// Verilen `<` konumunda `<(script|style)\b[^>]*>.*?</\1>` bloğu varsa bitiş byte'ını döndürür.
fn match_script_or_style_block(text: &str, lt_pos: usize) -> Option<usize> {
    let after_lt = &text[lt_pos + 1..];
    let (tag_len, close_tag) = if starts_with_ignore_ascii_case(after_lt, "script") {
        (6, "</script>")
    } else if starts_with_ignore_ascii_case(after_lt, "style") {
        (5, "</style>")
    } else {
        return None;
    };

    let after_tag = &after_lt[tag_len..];
    // `\b` kelime sınırı kontrolü (`<scripts>` gibi farklı etiketleri hariç tutar)
    let next_ch = after_tag.chars().next()?;
    if is_word_char(next_ch) {
        return None;
    }

    let gt_rel = after_tag.find('>')?;
    let body_start = lt_pos + 1 + tag_len + gt_rel + 1;
    let close_rel = find_ignore_ascii_case(&text[body_start..], close_tag)?;
    Some(body_start + close_rel + close_tag.len())
}

/// `<(script|style)\b[^>]*>.*?</\1>` bloklarını Python sırasıyla (`_HTML_TAG_RE` öncesinde) temizler.
fn strip_script_and_style(text: &str) -> Cow<'_, str> {
    if !text.as_bytes().contains(&b'<') {
        return Cow::Borrowed(text);
    }

    let mut out: Option<String> = None;
    let mut last_copied = 0usize;
    let mut i = 0usize;

    while let Some(rel) = text[i..].find('<') {
        let pos = i + rel;
        if let Some(block_end) = match_script_or_style_block(text, pos) {
            let buf = out.get_or_insert_with(|| String::with_capacity(text.len()));
            buf.push_str(&text[last_copied..pos]);
            buf.push(' ');
            last_copied = block_end;
            i = block_end;
        } else {
            i = pos + 1;
        }
    }

    match out {
        Some(mut buf) => {
            buf.push_str(&text[last_copied..]);
            Cow::Owned(buf)
        }
        None => Cow::Borrowed(text),
    }
}

/// HTML etiketlerini (`<[^>]+>`) ve kontrol karakterlerini temizler.
fn strip_html_tags_and_controls(text: &str) -> Cow<'_, str> {
    let needs_work = text.bytes().any(|b| b == b'<' || is_control_byte(b));
    if !needs_work {
        return Cow::Borrowed(text);
    }

    let mut out = String::with_capacity(text.len());
    let mut i = 0;

    while i < text.len() {
        let rest = &text[i..];
        let next_special_rel = rest
            .as_bytes()
            .iter()
            .position(|&b| b == b'<' || is_control_byte(b));

        let Some(rel) = next_special_rel else {
            out.push_str(rest);
            break;
        };

        out.push_str(&rest[..rel]);
        let pos = i + rel;
        let b = text.as_bytes()[pos];

        if b == b'<' {
            if let Some(gt_rel) = text[pos + 1..].find('>') {
                if gt_rel > 0 {
                    out.push(' ');
                    i = pos + 1 + gt_rel + 1;
                    continue;
                }
            }
            out.push('<');
            i = pos + 1;
        } else {
            // Kontrol karakterini (`is_control_byte`) çıktıya eklemeden atla
            i = pos + 1;
        }
    }

    Cow::Owned(out)
}

/// Satır sonu tire bölünmesini (`(\w)-\n(\w)` -> `\1\2`) Python `re.sub` uyumlu şekilde birleştirir.
fn merge_hyphen_wraps(text: &str) -> Cow<'_, str> {
    if !text.contains("-\n") {
        return Cow::Borrowed(text);
    }

    let mut out = String::with_capacity(text.len());
    let mut iter = text.char_indices().peekable();
    let mut prev_consumable_word = false;

    while let Some((idx, ch)) = iter.next() {
        if ch == '-' && prev_consumable_word {
            let after_hyphen = &text[idx + 1..];
            if let Some(rest_after_nl) = after_hyphen.strip_prefix('\n') {
                if let Some(next_word) = rest_after_nl.chars().next() {
                    if is_word_char(next_word) {
                        // `-\n` ikilisini atla ve `next_word` karakterini tüketerek yaz
                        iter.next(); // '\n'
                        if let Some((_, consumed_word)) = iter.next() {
                            out.push(consumed_word);
                        }
                        prev_consumable_word = false;
                        continue;
                    }
                }
            }
        }

        out.push(ch);
        prev_consumable_word = is_word_char(ch);
    }

    Cow::Owned(out)
}

/// Yatay boşlukları tek boşluğa indirir, satır kenarlarını kırpar ve 3+ boş satırı `\n\n` yapar.
fn normalize_whitespace_and_lines(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut has_emitted_line = false;
    let mut empty_lines_since_last = 0usize;

    for raw_line in text.split('\n') {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            if has_emitted_line {
                empty_lines_since_last += 1;
            }
            continue;
        }

        if has_emitted_line {
            if empty_lines_since_last == 0 {
                out.push('\n');
            } else {
                out.push_str("\n\n");
            }
        }

        let fast_clean = !trimmed.contains("  ")
            && !trimmed
                .chars()
                .any(|ch| ch != ' ' && (ch.is_whitespace() || is_control_char(ch)));
        if fast_clean {
            out.push_str(trimmed);
        } else {
            let mut in_ws = false;
            for ch in trimmed.chars() {
                if ch.is_whitespace() {
                    if !in_ws {
                        out.push(' ');
                        in_ws = true;
                    }
                } else {
                    out.push(ch);
                    in_ws = false;
                }
            }
        }

        has_emitted_line = true;
        empty_lines_since_last = 0;
    }

    out
}

/// HTML kalıntılarını, kontrol karakterlerini, tire bölünmelerini ve gereksiz boşlukları temizler.
pub fn clean_text_native(raw_text: &str) -> String {
    let step_eol = normalize_line_endings(raw_text);
    let step_script = strip_script_and_style(step_eol.as_ref());
    let step_html = strip_html_tags_and_controls(step_script.as_ref());
    let step_hyphen = merge_hyphen_wraps(step_html.as_ref());
    normalize_whitespace_and_lines(step_hyphen.as_ref())
}

#[inline]
fn char_at(text: &str, char_to_byte: &[usize], char_idx: usize) -> char {
    text[char_to_byte[char_idx]..char_to_byte[char_idx + 1]]
        .chars()
        .next()
        .unwrap_or('\0')
}

#[inline]
fn byte_to_char_idx(char_to_byte: &[usize], lo: usize, hi: usize, abs_byte: usize) -> usize {
    lo + char_to_byte[lo..hi]
        .binary_search(&abs_byte)
        .unwrap_or_else(|idx| idx)
}

/// Mümkün olduğunca cümle veya kelime sınırından temiz bitiş karakter indeksi seçer.
fn find_split_end(cleaned: &str, char_to_byte: &[usize], start: usize, max_end: usize) -> usize {
    let midpoint = start + (max_end - start) / 2;
    let window = &cleaned[char_to_byte[midpoint]..char_to_byte[max_end]];
    let mid_byte = char_to_byte[midpoint];

    for separator in [". ", "! ", "? ", "\n"] {
        if let Some(rel_byte) = window.rfind(separator) {
            let pos = byte_to_char_idx(char_to_byte, midpoint, max_end, mid_byte + rel_byte);
            return if separator == "\n" { pos } else { pos + 1 };
        }
    }

    if let Some(rel_byte) = window.rfind(' ') {
        let space_pos = byte_to_char_idx(char_to_byte, midpoint, max_end, mid_byte + rel_byte);
        if space_pos > start {
            return space_pos;
        }
    }

    max_end
}

/// Bir sonraki parça başlangıcını kelime ortasına düşmeyecek şekilde hizalar.
fn find_overlap_start(
    cleaned: &str,
    char_to_byte: &[usize],
    start: usize,
    end: usize,
    chunk_overlap: usize,
) -> usize {
    if chunk_overlap == 0 {
        return end;
    }

    let total_len = char_to_byte.len() - 1;
    let candidate = (start + 1).max(end.saturating_sub(chunk_overlap));
    if candidate > 0
        && candidate < total_len
        && !char_at(cleaned, char_to_byte, candidate - 1).is_whitespace()
    {
        let overlap_window = &cleaned[char_to_byte[candidate]..char_to_byte[end]];
        if let Some(rel_byte) = overlap_window.find(' ') {
            let next_space = byte_to_char_idx(
                char_to_byte,
                candidate,
                end,
                char_to_byte[candidate] + rel_byte,
            );
            if next_space + 1 < end {
                return next_space + 1;
            }
        }
    }

    candidate
}

/// Metni temizleyip zero-copy `&str` dilimleriyle cümle/kelime sınırlarına hizalı parçalara böler.
pub fn chunk_text_native(
    text: &str,
    chunk_size: usize,
    chunk_overlap: usize,
) -> Result<Vec<String>, &'static str> {
    if chunk_size == 0 {
        return Err("chunk_size must be positive");
    }
    if chunk_overlap >= chunk_size {
        return Err("chunk_overlap must be >= 0 and smaller than chunk_size");
    }

    let cleaned = clean_text_native(text);
    if cleaned.is_empty() {
        return Ok(Vec::new());
    }

    let mut char_to_byte: Vec<usize> = cleaned.char_indices().map(|(b, _)| b).collect();
    let total_len = char_to_byte.len();
    if total_len <= chunk_size {
        return Ok(vec![cleaned]);
    }
    char_to_byte.push(cleaned.len());

    let mut chunks = Vec::new();
    let mut start = 0usize;

    while start < total_len {
        let max_end = (start + chunk_size).min(total_len);
        let end = if max_end == total_len {
            total_len
        } else {
            find_split_end(&cleaned, &char_to_byte, start, max_end)
        };

        let piece = cleaned[char_to_byte[start]..char_to_byte[end]].trim();
        if !piece.is_empty() {
            chunks.push(piece.to_owned());
        }

        if end >= total_len {
            break;
        }

        let mut next_start = find_overlap_start(&cleaned, &char_to_byte, start, end, chunk_overlap);
        while next_start < total_len && char_at(&cleaned, &char_to_byte, next_start).is_whitespace()
        {
            next_start += 1;
        }
        start = if next_start > start { next_start } else { end };
    }

    Ok(chunks)
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

/// Ham doküman metnini GIL'i serbest bırakarak Rust'ta temizler.
#[pyfunction]
#[pyo3(signature = (raw_text))]
pub fn clean_text(py: Python<'_>, raw_text: &str) -> String {
    py.allow_threads(|| clean_text_native(raw_text))
}

/// Metni GIL'i serbest bırakarak cümle/kelime sınırlarına hizalı örtüşmeli parçalara böler.
#[pyfunction]
#[pyo3(signature = (text, chunk_size = 500, chunk_overlap = 75))]
pub fn chunk_text(
    py: Python<'_>,
    text: &str,
    chunk_size: isize,
    chunk_overlap: isize,
) -> PyResult<Vec<String>> {
    if chunk_size <= 0 {
        return Err(PyValueError::new_err("chunk_size must be positive"));
    }
    if chunk_overlap < 0 || chunk_overlap >= chunk_size {
        return Err(PyValueError::new_err(
            "chunk_overlap must be >= 0 and smaller than chunk_size",
        ));
    }

    let size = chunk_size as usize;
    let overlap = chunk_overlap as usize;
    py.allow_threads(|| chunk_text_native(text, size, overlap))
        .map_err(PyValueError::new_err)
}

/// `text_processor` Python native uzantı modülü tanımı.
#[pymodule]
fn text_processor(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", crate_version())?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(health_check, m)?)?;
    m.add_function(wrap_pyfunction!(count_chars, m)?)?;
    m.add_function(wrap_pyfunction!(clean_text, m)?)?;
    m.add_function(wrap_pyfunction!(chunk_text, m)?)?;
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

    #[test]
    fn test_clean_text_strips_script_style_html_control_and_hyphen_wraps() {
        let raw = concat!(
            "<SCRIPT type='text/javascript'>alert(1);\nconsole.log(2);</script>",
            "<STYLE>.a { color: red; }</STYLE>",
            "<p>Merhaba   dün-\nyası!</p>\x00\x07\x7f\r\n\r\n\r\n",
            "İkinci   satır\t\tşir-\nket   metni."
        );
        let cleaned = clean_text_native(raw);
        assert_eq!(cleaned, "Merhaba dünyası!\n\nİkinci satır şirket metni.");
    }

    #[test]
    fn test_clean_text_preserves_unclosed_angle_bracket_before_script() {
        let raw = "2 < 5 and <script>alert(1)</script> end";
        let cleaned = clean_text_native(raw);
        assert_eq!(cleaned, "2 < 5 and end");

        assert_eq!(clean_text_native("x < y and z"), "x < y and z");
        assert_eq!(clean_text_native("z <> w"), "z <> w");
        assert_eq!(clean_text_native("x < y and z <> w"), "x w");
    }

    #[test]
    fn test_chunk_text_splits_on_sentence_and_word_boundaries_with_overlap() {
        let mut sentences = Vec::new();
        for i in 1..15 {
            sentences.push(format!(
                "Cümle numarası {i} Türkçe sistem dokümantasyonu 🚀 için üretildi."
            ));
        }
        let text = sentences.join(" ");

        let chunks = chunk_text_native(&text, 140, 30).expect("chunking should succeed");
        assert!(chunks.len() > 2);
        assert!(chunks.iter().all(|c| {
            let count = c.chars().count();
            count > 0 && count <= 140
        }));

        let first_tail: Vec<&str> = chunks[0].split_whitespace().rev().take(4).collect();
        let second_head: Vec<&str> = chunks[1].split_whitespace().take(6).collect();
        assert!(first_tail.iter().any(|word| second_head.contains(word)));
    }

    #[test]
    fn test_chunk_text_validates_invalid_parameters_and_empty_inputs() {
        assert_eq!(
            chunk_text_native("   \n\t  ", 100, 10).unwrap(),
            Vec::<String>::new()
        );
        assert_eq!(
            chunk_text_native("Kısa Türkçe metin.", 100, 10).unwrap(),
            vec!["Kısa Türkçe metin."]
        );
        assert!(chunk_text_native("abc", 0, 0).is_err());
        assert!(chunk_text_native("abc", 100, 100).is_err());
        assert!(chunk_text_native("abc", 50, 75).is_err());
    }
}
