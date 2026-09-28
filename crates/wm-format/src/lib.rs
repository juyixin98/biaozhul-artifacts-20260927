//! Versioned binary container for [`wm_core::WaveletMatrix`].
//!
//! On-disk layout (all integers little-endian):
//!
//! ```text
//! offset  size           field
//! 0       4              magic "WMXB"
//! 4       4              format version (u32, currently 1)
//! 8       8              payload length in bytes (u64)
//! 16      <payload_len>  payload
//! 16+plen 8              FNV-1a 64 checksum over bytes [0, 16+plen)
//! ```
//!
//! Payload:
//!
//! ```text
//! n                  u64
//! bit_len            u32
//! distinct_count     u64
//! values             distinct_count * i64 (ascending)
//! per level:
//!     zero_count     u64
//!     word_count     u64
//!     words          word_count * u64
//! ```
//!
//! Decoding verifies magic, version, length, checksum and structural
//! consistency, turning every corruption mode into a specific
//! [`FormatError`] category instead of a panic.

use wm_core::bitvec::BitVector;
use wm_core::wavelet::InvalidParts;
use wm_core::WaveletMatrix;

/// Current format version.
pub const FORMAT_VERSION: u32 = 1;
const MAGIC: &[u8; 4] = b"WMXB";
const HEADER_LEN: usize = 16;

/// Binary (de)serialization failure categories.
#[derive(Debug, thiserror::Error)]
pub enum FormatError {
    /// File shorter than the 16-byte header.
    #[error("file too short: {0} bytes, need at least {HEADER_LEN}")]
    TooShort(usize),
    /// Magic bytes do not match `WMXB`.
    #[error("bad magic bytes; not a wavelet matrix index")]
    BadMagic,
    /// Format version is unsupported.
    #[error("unsupported format version {found}; this build supports up to {max}")]
    UnsupportedVersion { found: u32, max: u32 },
    /// Declared payload length runs past the file / checksum is missing.
    #[error("declared payload length {declared} inconsistent with file size {file_len}")]
    LengthMismatch { declared: u64, file_len: usize },
    /// FNV-1a checksum mismatch.
    #[error("checksum mismatch: stored {stored:#018x}, computed {computed:#018x}")]
    ChecksumMismatch { stored: u64, computed: u64 },
    /// Payload declared a count that exceeds the remaining bytes.
    #[error("truncated payload while reading {what}: needed {needed}, have {remaining}")]
    Truncated {
        what: &'static str,
        needed: usize,
        remaining: usize,
    },
    /// A decoded count exceeded sane limits (u64 -> usize on 32-bit, etc.).
    #[error("field {what} = {value} exceeds addressable limit {limit}")]
    TooLarge {
        what: &'static str,
        value: u64,
        limit: u64,
    },
    /// Fields are internally inconsistent or fail kernel validation.
    #[error("structural validation failed: {0}")]
    InvalidStructure(String),
}

fn fnv1a64(bytes: &[u8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    fn take(&mut self, n: usize, what: &'static str) -> Result<&'a [u8], FormatError> {
        if self.pos + n > self.buf.len() {
            return Err(FormatError::Truncated {
                what,
                needed: n,
                remaining: self.buf.len().saturating_sub(self.pos),
            });
        }
        let out = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    fn u64(&mut self, what: &'static str) -> Result<u64, FormatError> {
        let b = self.take(8, what)?;
        Ok(u64::from_le_bytes(b.try_into().unwrap()))
    }

    fn u32(&mut self, what: &'static str) -> Result<u32, FormatError> {
        let b = self.take(4, what)?;
        Ok(u32::from_le_bytes(b.try_into().unwrap()))
    }

    fn i64(&mut self, what: &'static str) -> Result<i64, FormatError> {
        let b = self.take(8, what)?;
        Ok(i64::from_le_bytes(b.try_into().unwrap()))
    }
}

fn as_usize(what: &'static str, value: u64) -> Result<usize, FormatError> {
    usize::try_from(value).map_err(|_| FormatError::TooLarge {
        what,
        value,
        limit: usize::MAX as u64,
    })
}

/// Serialize an index into the versioned binary container.
#[must_use]
pub fn encode(wm: &WaveletMatrix) -> Vec<u8> {
    let p = wm.parts();
    let mut payload: Vec<u8> = Vec::new();
    payload.extend_from_slice(&(p.n as u64).to_le_bytes());
    payload.extend_from_slice(&(wm.bit_len() as u32).to_le_bytes());
    payload.extend_from_slice(&(p.values.len() as u64).to_le_bytes());
    for v in p.values {
        payload.extend_from_slice(&v.to_le_bytes());
    }
    for (level, bv) in p.levels.iter().enumerate() {
        payload.extend_from_slice(&(p.zero_counts[level] as u64).to_le_bytes());
        payload.extend_from_slice(&(bv.words().len() as u64).to_le_bytes());
        for w in bv.words() {
            payload.extend_from_slice(&w.to_le_bytes());
        }
    }

    let mut out = Vec::with_capacity(HEADER_LEN + payload.len() + 8);
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    out.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    out.extend_from_slice(&payload);
    let checksum = fnv1a64(&out);
    out.extend_from_slice(&checksum.to_le_bytes());
    out
}

/// Parse and validate an index from the binary container.
pub fn decode(bytes: &[u8]) -> Result<WaveletMatrix, FormatError> {
    if bytes.len() < HEADER_LEN + 8 {
        return Err(FormatError::TooShort(bytes.len()));
    }
    if &bytes[0..4] != MAGIC {
        return Err(FormatError::BadMagic);
    }
    let version = u32::from_le_bytes(bytes[4..8].try_into().unwrap());
    if version != FORMAT_VERSION {
        return Err(FormatError::UnsupportedVersion {
            found: version,
            max: FORMAT_VERSION,
        });
    }
    let payload_len = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
    let total = HEADER_LEN as u64 + payload_len + 8;
    if total != bytes.len() as u64 {
        return Err(FormatError::LengthMismatch {
            declared: payload_len,
            file_len: bytes.len(),
        });
    }
    let body_end = HEADER_LEN + payload_len as usize;
    let stored_checksum = u64::from_le_bytes(bytes[body_end..body_end + 8].try_into().unwrap());
    let computed = fnv1a64(&bytes[..body_end]);
    if stored_checksum != computed {
        return Err(FormatError::ChecksumMismatch {
            stored: stored_checksum,
            computed,
        });
    }

    let payload = &bytes[HEADER_LEN..body_end];
    let mut cur = Cursor::new(payload);

    let n = as_usize("n", cur.u64("n")?)?;
    let bit_len = cur.u32("bit_len")? as usize;
    let distinct_count = as_usize("distinct_count", cur.u64("distinct_count")?)?;

    let mut values = Vec::with_capacity(distinct_count);
    for _ in 0..distinct_count {
        values.push(cur.i64("value")?);
    }

    let mut levels = Vec::with_capacity(bit_len);
    let mut zero_counts = Vec::with_capacity(bit_len);
    for level in 0..bit_len {
        let zeros = as_usize("zero_count", cur.u64("zero_count")?)?;
        let word_count = as_usize("word_count", cur.u64("word_count")?)?;
        let mut words = Vec::with_capacity(word_count);
        for _ in 0..word_count {
            words.push(cur.u64("word")?);
        }
        let bv = BitVector::try_from_words(n, words)
            .map_err(|e| FormatError::InvalidStructure(format!("level {level}: {e:?}")))?;
        levels.push(bv);
        zero_counts.push(zeros);
    }

    // Every payload byte must be consumed (no trailing garbage).
    if cur.pos != payload.len() {
        return Err(FormatError::InvalidStructure(format!(
            "{} trailing payload bytes",
            payload.len() - cur.pos
        )));
    }

    WaveletMatrix::from_parts(levels, zero_counts, values, n)
        .map_err(|e: InvalidParts| FormatError::InvalidStructure(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_each_corruption_mode() {
        let wm = WaveletMatrix::build(&[-5, 7, 7, i64::MAX, 0]).unwrap();
        let good = encode(&wm);

        assert!(decode(&good).is_ok());

        // Truncation at several points.
        assert!(matches!(decode(&[]), Err(FormatError::TooShort(0))));
        assert!(matches!(
            decode(&good[..10]),
            Err(FormatError::TooShort(10))
        ));

        // Bad magic.
        let mut bad = good.clone();
        bad[0] = b'X';
        assert!(matches!(decode(&bad), Err(FormatError::BadMagic)));

        // Unknown version.
        let mut bad = good.clone();
        bad[4..8].copy_from_slice(&999u32.to_le_bytes());
        assert!(matches!(
            decode(&bad),
            Err(FormatError::UnsupportedVersion { found: 999, .. })
        ));

        // Declared payload length too large.
        let mut bad = good.clone();
        bad[8..16].copy_from_slice(&(1u64 << 40).to_le_bytes());
        assert!(matches!(
            decode(&bad),
            Err(FormatError::LengthMismatch { .. })
        ));

        // Flip one payload bit -> checksum mismatch before anything else.
        let mut bad = good.clone();
        let byte = HEADER_LEN + 5;
        bad[byte] ^= 0x01;
        assert!(matches!(
            decode(&bad),
            Err(FormatError::ChecksumMismatch { .. })
        ));

        // Flip a byte and patch the checksum so structural validation runs.
        let mut bad = good.clone();
        bad[byte] ^= 0x01;
        let end = bad.len() - 8;
        let fixed = fnv1a64(&bad[..end]);
        bad[end..].copy_from_slice(&fixed.to_le_bytes());
        assert!(matches!(
            decode(&bad),
            Err(FormatError::InvalidStructure(_))
        ));
    }

    #[test]
    fn zero_level_single_distinct_round_trip() {
        let wm = WaveletMatrix::build(&[11i64; 3]).unwrap();
        let bytes = encode(&wm);
        let back = decode(&bytes).unwrap();
        assert_eq!(back, wm);
        assert_eq!(back.quantile(0, 3, 1).unwrap(), 11);
    }
}
