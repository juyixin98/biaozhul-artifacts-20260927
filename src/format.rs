//! Binary persistence format for [`WmIndex`].
//!
//! Wire layout (all integers little-endian):
//!
//! ```text
//! +----------------+-----------------------------------------------------+
//! | magic [4]      | b"WMX1"                                             |
//! | version u32    | currently 1                                         |
//! | payload_len u64| bytes in the payload below                          |
//! | payload        | len u64 | distinct u64 | height u32                 |
//! |                | distinct * i64  (sorted value table)                |
//! |                | height * { zeros u64 | bits_len u64 | words:        |
//! |                |            ceil(bits_len/64) * u64 }                |
//! | checksum u64   | FNV-1a 64 of the payload bytes                      |
//! +----------------+-----------------------------------------------------+
//! ```
//!
//! The rank prefix table of each bit vector is deliberately not stored: it
//! is derived from the packed words on load, so it cannot desynchronize.

use crate::bitvector::BitVector;
use crate::error::WmError;
use crate::index::WmIndex;
use crate::wavelet_matrix::Level;

pub const MAGIC: [u8; 4] = *b"WMX1";
pub const FORMAT_VERSION: u32 = 1;

/// Encode an index into the canonical byte representation.
pub fn encode(index: &WmIndex) -> Vec<u8> {
    let mut p = Payload::new();
    p.u64(index.len() as u64);
    p.u64(index.value_table().len() as u64);
    p.u32(index.height() as u32);
    for &v in index.value_table() {
        p.i64(v);
    }
    for lvl in index.levels() {
        p.u64(lvl.zeros as u64);
        p.u64(lvl.bv.len() as u64);
        for w in lvl.bv.words() {
            p.u64(*w);
        }
    }
    let payload = p.into_bytes();

    let mut out = Vec::with_capacity(4 + 4 + 8 + payload.len() + 8);
    out.extend_from_slice(&MAGIC);
    out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    out.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    out.extend_from_slice(&payload);
    out.extend_from_slice(&fnv1a64(&payload).to_le_bytes());
    out
}

/// Decode and validate an index. Any structural problem is reported as
/// [`WmError::CorruptFormat`], an unknown version as
/// [`WmError::UnsupportedVersion`].
pub fn decode(bytes: &[u8]) -> Result<WmIndex, WmError> {
    let mut r = Reader::new(bytes);
    let mut magic = [0u8; 4];
    magic.copy_from_slice(r.take(4)?);
    if magic != MAGIC {
        return Err(WmError::CorruptFormat("bad magic".into()));
    }
    let version = r.u32()?;
    if version != FORMAT_VERSION {
        return Err(WmError::UnsupportedVersion(version));
    }
    let payload_len = r.u64()? as usize;
    let payload = r.take(payload_len)?;
    let checksum = r.u64()?;
    if fnv1a64(payload) != checksum {
        return Err(WmError::CorruptFormat("payload checksum mismatch".into()));
    }
    if r.remaining() != 0 {
        return Err(WmError::CorruptFormat(format!(
            "{} trailing byte(s) after record",
            r.remaining()
        )));
    }

    let mut pr = Reader::new(payload);
    let len = pr.u64()? as usize;
    let distinct = pr.u64()? as usize;
    let height = pr.u32()? as usize;
    if len == 0 {
        return Err(WmError::CorruptFormat("zero sequence length".into()));
    }
    if distinct == 0 {
        return Err(WmError::CorruptFormat("zero distinct values".into()));
    }
    if !(1..=64).contains(&height) {
        return Err(WmError::CorruptFormat(format!(
            "height {height} outside [1, 64]"
        )));
    }
    let mut values = Vec::with_capacity(distinct);
    for _ in 0..distinct {
        values.push(pr.i64()?);
    }
    let mut levels = Vec::with_capacity(height);
    for d in 0..height {
        let zeros = pr.u64()? as usize;
        let bits_len = pr.u64()? as usize;
        if bits_len != len {
            return Err(WmError::CorruptFormat(format!(
                "level {d} length {bits_len} != sequence length {len}"
            )));
        }
        let word_count = bits_len.div_ceil(64);
        let mut words = Vec::with_capacity(word_count);
        for _ in 0..word_count {
            words.push(pr.u64()?);
        }
        // Any bits above bits_len must be zero: otherwise the stored words
        // carry data that the length claims does not exist.
        if !bits_len.is_multiple_of(64) {
            let tail_mask = (1u64 << (bits_len % 64)) - 1;
            if words[word_count - 1] & !tail_mask != 0 {
                return Err(WmError::CorruptFormat(format!(
                    "level {d} has set bits past its length"
                )));
            }
        }
        let _ = zeros; // validated together with the level by WmIndex::restore
        levels.push(Level {
            bv: BitVector::from_words(words, bits_len),
            zeros,
        });
    }
    if pr.remaining() != 0 {
        return Err(WmError::CorruptFormat(format!(
            "{} trailing payload byte(s)",
            pr.remaining()
        )));
    }

    WmIndex::restore(values, height, levels, len)
}

struct Payload {
    buf: Vec<u8>,
}

impl Payload {
    fn new() -> Self {
        Payload { buf: Vec::new() }
    }
    fn u64(&mut self, v: u64) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn u32(&mut self, v: u32) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn i64(&mut self, v: i64) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn into_bytes(self) -> Vec<u8> {
        self.buf
    }
}

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }
    fn take(&mut self, n: usize) -> Result<&'a [u8], WmError> {
        if self.pos + n > self.buf.len() {
            return Err(WmError::CorruptFormat(format!(
                "unexpected end of data at byte {} (needed {n})",
                self.pos
            )));
        }
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }
    fn u64(&mut self) -> Result<u64, WmError> {
        let mut a = [0u8; 8];
        a.copy_from_slice(self.take(8)?);
        Ok(u64::from_le_bytes(a))
    }
    fn u32(&mut self) -> Result<u32, WmError> {
        let mut a = [0u8; 4];
        a.copy_from_slice(self.take(4)?);
        Ok(u32::from_le_bytes(a))
    }
    fn i64(&mut self) -> Result<i64, WmError> {
        let mut a = [0u8; 8];
        a.copy_from_slice(self.take(8)?);
        Ok(i64::from_le_bytes(a))
    }
    fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }
}

/// FNV-1a 64-bit checksum (http://www.isthe.com/chongo/tech/comp/fnv/).
fn fnv1a64(data: &[u8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_fnv_vector() {
        // Standard FNV-1a test vector.
        assert_eq!(fnv1a64(b""), 0xcbf29ce484222325);
        assert_eq!(fnv1a64(b"a"), 0xaf63dc4c8601ec8c);
    }

    #[test]
    fn truncation_and_flipped_byte_are_rejected() {
        let idx = WmIndex::build(&[1, -1, 0, 3]).unwrap();
        let bytes = encode(&idx);

        assert!(decode(&bytes).is_ok());
        assert!(matches!(
            decode(&bytes[..bytes.len() - 1]),
            Err(WmError::CorruptFormat(_))
        ));

        let mut flipped = bytes.clone();
        // Flip a bit inside the payload (after the 16-byte header).
        flipped[20] ^= 0x01;
        assert!(matches!(decode(&flipped), Err(WmError::CorruptFormat(_))));
    }
}
