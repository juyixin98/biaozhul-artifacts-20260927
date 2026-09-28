//! Reference oracle: exhaustive scan of the original text.
//!
//! This is deliberately **not** an FM-index query — it is the ground truth
//! that tests compare the index against and the implementation behind the
//! `/verify` endpoint. It performs a literal, overlap-preserving scan.
//!
//! Empty-pattern semantics mirror [`crate::fm::FmIndex::search`]: every suffix
//! boundary matches, i.e. offsets `0..=text.len()`.

/// Return every starting offset where `pattern` occurs in `text`, overlaps
/// included, ascending.
pub fn naive_scan(text: &[u8], pattern: &[u8]) -> Vec<u64> {
    if pattern.is_empty() {
        return (0..=text.len() as u64).collect();
    }
    if pattern.len() > text.len() {
        return vec![];
    }
    let last = text.len() - pattern.len();
    (0..=last)
        .filter(|&i| text[i..i + pattern.len()] == *pattern)
        .map(|i| i as u64)
        .collect()
}

/// Simple text statistics used by verify responses.
pub fn text_stats(text: &[u8]) -> TextStats {
    let mut byte_freq = [0u64; 256];
    for &b in text {
        byte_freq[b as usize] += 1;
    }
    TextStats {
        length: text.len() as u64,
        zero_bytes: byte_freq[0],
        distinct_bytes: byte_freq.iter().filter(|c| **c > 0).count() as u64,
        byte_freq,
    }
}

#[derive(Debug, Clone)]
pub struct TextStats {
    pub length: u64,
    pub zero_bytes: u64,
    pub distinct_bytes: u64,
    pub byte_freq: [u64; 256],
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_preserves_overlaps() {
        assert_eq!(naive_scan(b"aaaa", b"aa"), vec![0, 1, 2]);
        assert_eq!(naive_scan(b"ababab", b"aba"), vec![0, 2]);
    }

    #[test]
    fn scan_edge_cases() {
        assert_eq!(naive_scan(b"", b""), vec![0]);
        assert_eq!(naive_scan(b"", b"x"), Vec::<u64>::new());
        assert_eq!(naive_scan(b"abc", b""), vec![0, 1, 2, 3]);
        assert_eq!(naive_scan(b"abc", b"abcd"), Vec::<u64>::new());
        assert_eq!(naive_scan(b"\x00\x00\xff", &[0x00]), vec![0, 1]);
    }
}
