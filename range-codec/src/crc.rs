//! CRC-32/ISO-HDLC (polynomial 0xEDB88320, reflected, init/xorout `0xFFFFFFFF`).
//!
//! Self-contained table-driven implementation so the container has no extra
//! dependency; matches the checksum produced by Python `zlib.crc32`.

#[derive(Debug, Clone)]
pub struct Crc32 {
    state: u32,
}

const TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
            k += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
};

impl Crc32 {
    pub fn new() -> Self {
        Crc32 { state: 0xFFFF_FFFF }
    }

    pub fn update(&mut self, bytes: &[u8]) {
        for &b in bytes {
            let idx = ((self.state ^ b as u32) & 0xFF) as usize;
            self.state = TABLE[idx] ^ (self.state >> 8);
        }
    }

    pub fn finish(self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }

    /// One-shot checksum.
    pub fn checksum(bytes: &[u8]) -> u32 {
        let mut c = Crc32::new();
        c.update(bytes);
        c.finish()
    }
}

impl Default for Crc32 {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        // Vectors cross-checked with zlib's CRC-32.
        assert_eq!(Crc32::checksum(b""), 0x0000_0000);
        assert_eq!(Crc32::checksum(b"123456789"), 0xCBF4_3926);
        assert_eq!(Crc32::checksum(b"a"), 0xE8B7_BE43);
    }

    #[test]
    fn incremental_equals_oneshot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let mut c = Crc32::new();
        for chunk in data.chunks(7) {
            c.update(chunk);
        }
        assert_eq!(c.finish(), Crc32::checksum(data));
    }
}
