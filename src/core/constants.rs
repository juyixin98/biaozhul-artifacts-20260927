//! Fixed format constants. "Fixed" is a correctness property of this backend:
//! a block never negotiates these values, so an attacker cannot request a
//! larger window or match length through the wire format.

/// On-disk magic: "LZ7B".
pub const MAGIC: [u8; 4] = *b"LZ7B";
/// Header layout version understood by this build.
pub const FORMAT_VERSION: u8 = 1;
/// Size in bytes of the fixed block header.
pub const HEADER_LEN: usize = 4 /*magic*/ + 1 /*version*/ + 1 /*frame type*/ + 4 /*index*/
    + 8 /*prev digest*/ + 4 /*payload crc*/ + 8 /*declared decompressed len*/;

/// Sliding dictionary window size in bytes (distance range `1..=WINDOW_SIZE`).
pub const WINDOW_SIZE: usize = 4096;
/// Minimum encodable match length.
pub const MIN_MATCH: usize = 3;
/// Maximum encodable match length (a match token carries length-MIN_MATCH on 16 bits).
pub const MAX_MATCH: usize = MIN_MATCH + u16::MAX as usize; // 65538
/// Hard cap on a single compressed payload accepted by a decoder.
pub const MAX_PAYLOAD: usize = 64 * 1024;
/// Hard cap on decompressed output of a single block.
pub const MAX_OUTPUT: usize = 1024 * 1024;
/// Maximum decompressed/compressed size ratio allowed for one block.
///
/// This is defense-in-depth on top of the absolute [`MAX_OUTPUT`] cap, not a
/// substitute for it. The value is deliberately generous: an honest LZ block of
/// a single repeated byte can legitimately reach ratios in the low tens of
/// thousands (e.g. the full 1 MiB output cap from a ~40-byte payload). Setting
/// the limit at 200_000 admits every block the fixed-window encoder can produce
/// while still refusing a pathological one-byte-payload / megabyte-output
/// claim before allocation.
pub const MAX_EXPANSION: usize = 200_000;
/// Largest input accepted from the compress HTTP/store path for one block.
pub const MAX_ENCODE_INPUT: usize = MAX_OUTPUT;

/// Hash-chain encoder tuning (not wire-visible).
pub const HASH_BITS: usize = 14;
pub const HASH_SIZE: usize = 1 << HASH_BITS;
pub const MAX_CHAIN: usize = 32;

/// Default aggregate cap for a [`crate::store::BlockStore`] (bytes on disk).
pub const DEFAULT_STORE_CAP: u64 = 64 * 1024 * 1024;
/// Default aggregate cap on the *decompressed* bytes of one stream. This is
/// the multi-block analogue of [`MAX_OUTPUT`]: a chain of small bomb blocks
/// must not be allowed to materialize gigabytes on a single decode request.
pub const DEFAULT_STREAM_OUTPUT_CAP: u64 = 16 * 1024 * 1024;

/// Prefix used for block file names: `block-00000007.lzb`.
pub const BLOCK_PREFIX: &str = "block-";
/// Block file suffix.
pub const BLOCK_SUFFIX: &str = ".lzb";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_layout_is_fixed() {
        assert_eq!(HEADER_LEN, 30);
        assert_eq!(MAGIC, *b"LZ7B");
        const {
            assert!(MIN_MATCH >= 3);
            assert!(MAX_MATCH > MIN_MATCH);
            assert!(MAX_EXPANSION >= 1);
        };
    }
}
