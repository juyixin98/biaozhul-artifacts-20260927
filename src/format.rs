//! On-disk binary format: constants, big-endian helpers, CRC-32.
//!
//! All multi-byte integers are stored **big-endian** (network byte order),
//! including the range-coded payload produced by the encoder, which emits
//! its digits most-significant-byte first.

/// Container magic: ASCII "RCMP".
pub const MAGIC: [u8; 4] = *b"RCMP";
/// Container format version implemented by this crate.
pub const FORMAT_VERSION: u16 = 1;
/// Header length in bytes (fixed for version 1):
/// 4 magic + 2 version + 2 flags + 4 bound + 4 alphabet + 8 declared + 4 crc.
pub const HEADER_LEN: usize = 28;

// Segment marker bytes.
/// Frequency-table segment: starts a new epoch.
pub const SEG_TABLE: u8 = b'T';
/// Coded chunk segment: symbols for one epoch.
pub const SEG_CHUNK: u8 = b'C';
/// Final segment: symbol-count terminator.
pub const SEG_EOF: u8 = b'E';

// Flag bits (header).
/// Input was produced with the adaptive model (epochs may be > 1).
pub const FLAG_ADAPTIVE: u16 = 0x0001;
/// All flag bits this version knows about.
pub const KNOWN_FLAGS: u16 = FLAG_ADAPTIVE;

// ---------------------------------------------------------------------------
// Big-endian readers/writers
// ---------------------------------------------------------------------------

/// Append big-endian integers to a byte buffer.
pub trait BeWrite {
    fn write_u16(&mut self, v: u16);
    fn write_u32(&mut self, v: u32);
    fn write_u64(&mut self, v: u64);
}

impl BeWrite for Vec<u8> {
    fn write_u16(&mut self, v: u16) {
        self.extend_from_slice(&v.to_be_bytes());
    }
    fn write_u32(&mut self, v: u32) {
        self.extend_from_slice(&v.to_be_bytes());
    }
    fn write_u64(&mut self, v: u64) {
        self.extend_from_slice(&v.to_be_bytes());
    }
}

/// Bounds-checked big-endian reader over a slice.
#[derive(Debug)]
pub struct BeReader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> BeReader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    pub fn position(&self) -> usize {
        self.pos
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn read_u8(&mut self) -> Option<u8> {
        let b = *self.buf.get(self.pos)?;
        self.pos += 1;
        Some(b)
    }

    pub fn read_bytes(&mut self, n: usize) -> Option<&'a [u8]> {
        let end = self.pos.checked_add(n)?;
        let s = self.buf.get(self.pos..end)?;
        self.pos = end;
        Some(s)
    }

    pub fn read_u16(&mut self) -> Option<u16> {
        self.read_bytes(2).map(|s| u16::from_be_bytes([s[0], s[1]]))
    }

    pub fn read_u32(&mut self) -> Option<u32> {
        self.read_bytes(4).map(|s| {
            let mut a = [0u8; 4];
            a.copy_from_slice(s);
            u32::from_be_bytes(a)
        })
    }

    pub fn read_u64(&mut self) -> Option<u64> {
        self.read_bytes(8).map(|s| {
            let mut a = [0u8; 8];
            a.copy_from_slice(s);
            u64::from_be_bytes(a)
        })
    }
}

// ---------------------------------------------------------------------------
// CRC-32 (IEEE 802.3, poly 0xEDB88320 reflected)
// ---------------------------------------------------------------------------

/// Standard CRC-32 checksum (same polynomial as zlib/gzip/PNG, no final XOR
/// complement call site ambiguity: this returns the fully finalized value).
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                0xEDB8_8320 ^ (crc >> 1)
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_known_vectors() {
        // The two canonical vectors for IEEE CRC-32 (final XOR included).
        assert_eq!(crc32(b""), 0x0000_0000);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn crc32_is_deterministic_and_position_independent() {
        assert_eq!(crc32(b"abc"), crc32(b"abc"));
        assert_ne!(crc32(b"abc"), crc32(b"abd"));
    }

    #[test]
    fn be_roundtrip() {
        let mut v = Vec::new();
        v.write_u16(0x0102);
        v.write_u32(0x0304_0506);
        v.write_u64(0x0708_090A_0B0C_0D0E);
        assert_eq!(v, vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]);
        let mut r = BeReader::new(&v);
        assert_eq!(r.read_u16(), Some(0x0102));
        assert_eq!(r.read_u32(), Some(0x0304_0506));
        assert_eq!(r.read_u64(), Some(0x0708_090A_0B0C_0D0E));
        assert_eq!(r.remaining(), 0);
        assert_eq!(r.read_u8(), None);
    }
}
