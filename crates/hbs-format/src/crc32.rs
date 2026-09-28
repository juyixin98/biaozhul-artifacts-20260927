//! IEEE CRC-32 (polynomial 0xEDB88320), the same checksum used in PNG/zlib.
//!
//! Implemented from scratch with a `const` table so there is no external
//! dependency; incrementally feedable for streaming encoders.

/// CRC-32 lookup table generated at compile time.
pub const TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut n = 0usize;
    while n < 256 {
        let mut c = n as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                0xEDB88320 ^ (c >> 1)
            } else {
                c >> 1
            };
            k += 1;
        }
        table[n] = c;
        n += 1;
    }
    table
};

/// Streaming CRC-32 writer.
#[derive(Debug, Clone)]
pub struct Crc32 {
    state: u32,
}

impl Default for Crc32 {
    fn default() -> Self {
        Self { state: 0xFFFF_FFFF }
    }
}

impl Crc32 {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update(&mut self, bytes: &[u8]) {
        for &b in bytes {
            let idx = ((self.state ^ b as u32) & 0xFF) as usize;
            self.state = TABLE[idx] ^ (self.state >> 8);
        }
    }

    pub fn finish(&self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }
}

/// One-shot CRC-32 of a slice.
pub fn crc32(bytes: &[u8]) -> u32 {
    let mut c = Crc32::new();
    c.update(bytes);
    c.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        // RFC 1952 / zlib reference values.
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"a"), 0xE8B7_BE43);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn streaming_matches_oneshot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let mut c = Crc32::new();
        for chunk in data.as_chunks::<7>().0 {
            c.update(chunk);
        }
        let rem = &data[(data.len() / 7) * 7..];
        c.update(rem);
        assert_eq!(c.finish(), crc32(data));
    }
}
