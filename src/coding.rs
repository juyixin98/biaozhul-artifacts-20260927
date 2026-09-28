//! Symbol coding: the unique sentinel and the byte alphabet.
//!
//! The text is a raw byte string, so byte value `0x00` is legitimate data and
//! cannot be reused as the end-of-string sentinel. We therefore work over an
//! alphabet of **257 symbols**:
//!
//! ```text
//!   sentinel          -> symbol 0       (unique, never produced by a byte)
//!   text byte b       -> symbol b + 1   (1..=256)
//! ```
//!
//! The coded text always ends with exactly one sentinel. This convention is
//! shared by the suffix-array construction, BWT, backwards search and the
//! persistence layer, so the invariant can be re-checked on load.

/// Number of symbols in the coded alphabet (sentinel + 256 byte values).
pub const ALPHABET_SIZE: usize = 257;

/// The unique sentinel symbol.
pub const SENTINEL: u16 = 0;

/// Encode one raw text byte into its coded symbol.
#[inline]
pub fn encode_byte(b: u8) -> u16 {
    b as u16 + 1
}

/// Decode a coded symbol back to its byte, or [`None`] for the sentinel.
#[inline]
pub fn decode_symbol(s: u16) -> Option<u8> {
    match s {
        SENTINEL => None,
        1..=256 => Some((s - 1) as u8),
        _ => None,
    }
}

/// Code `text` and append the unique terminal sentinel.
pub fn code_with_sentinel(text: &[u8]) -> Vec<u16> {
    let mut out = Vec::with_capacity(text.len() + 1);
    out.extend(text.iter().map(|&b| encode_byte(b)));
    out.push(SENTINEL);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_byte_does_not_collide_with_sentinel() {
        // The key invariant motivating this module: 0x00 is data.
        assert_ne!(encode_byte(0x00), SENTINEL);
        assert_eq!(encode_byte(0x00), 1);
        assert_eq!(encode_byte(0xFF), 256);
        assert_eq!(decode_symbol(1), Some(0x00));
        assert_eq!(decode_symbol(256), Some(0xFF));
        assert_eq!(decode_symbol(SENTINEL), None);
    }

    #[test]
    fn round_trip_and_sentinel_placement() {
        let coded = code_with_sentinel(b"ab\x00\xff");
        assert_eq!(coded, vec![b'a' as u16 + 1, b'b' as u16 + 1, 1, 256, 0]);
        assert_eq!(*coded.last().unwrap(), SENTINEL);
        assert_eq!(coded.iter().filter(|&&s| s == SENTINEL).count(), 1);
    }
}
