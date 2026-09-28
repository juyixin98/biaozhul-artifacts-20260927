//! CRC-32 (IEEE 802.3, reflected polynomial `0xEDB8_8320`, init/final XOR `0xFFFF_FFFF`).
//!
//! Used for three independent integrity fields in the container:
//! directory CRC, per-block payload CRC and per-block original-data CRC.
//! The lookup table is generated fully at compile time, so there are no
//! dependencies beyond `core` and no runtime one-time initialisation.

/// Reflected CRC-32 polynomial, same as zlib/DEFLATE.
const POLY: u32 = 0xEDB8_8320;

/// Build the 256-entry table as a `const` so it ends up in read-only data.
const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut crc = i as u32;
        let mut j = 0;
        while j < 8 {
            // Branch-free reflected bit step.
            crc = (crc >> 1) ^ (POLY & ((crc & 1).wrapping_neg()));
            j += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
}

const TABLE: [u32; 256] = build_table();

/// Compute the CRC-32 of `bytes`.
pub fn checksum(bytes: &[u8]) -> u32 {
    let mut crc = u32::MAX;
    for &b in bytes {
        let idx = ((crc ^ b as u32) & 0xFF) as usize;
        crc = (crc >> 8) ^ TABLE[idx];
    }
    crc ^ u32::MAX
}

/// Streaming CRC-32 state, for hashing a block as it is encoded/decoded
/// without materialising an extra contiguous buffer.
#[derive(Debug, Clone)]
pub struct Crc32 {
    state: u32,
}

impl Default for Crc32 {
    fn default() -> Self {
        Self::new()
    }
}

impl Crc32 {
    /// Start a fresh CRC.
    pub fn new() -> Self {
        Crc32 { state: u32::MAX }
    }

    /// Fold another chunk into the running CRC.
    pub fn update(&mut self, bytes: &[u8]) {
        for &b in bytes {
            let idx = ((self.state ^ b as u32) & 0xFF) as usize;
            self.state = (self.state >> 8) ^ TABLE[idx];
        }
    }

    /// Finalise and return the checksum (state is not consumed, but the
    /// result is only meaningful once).
    pub fn finish(&self) -> u32 {
        self.state ^ u32::MAX
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Canonical test vector: CRC-32(b"123456789") == 0xCBF43926.
    #[test]
    fn known_answer_check_value() {
        assert_eq!(checksum(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn empty_is_zero() {
        assert_eq!(checksum(b""), 0);
    }

    #[test]
    fn streaming_matches_one_shot() {
        let data = b"canonical-huffman-crc32";
        let mut crc = Crc32::new();
        crc.update(&data[..7]);
        crc.update(&data[7..]);
        assert_eq!(crc.finish(), checksum(data));
    }
}
