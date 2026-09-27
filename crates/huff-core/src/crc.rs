//! IEEE CRC-32 (`zlib` polynomial 0xEDB88320, init `0xFFFF_FFFF`, final XOR).

use crate::error::{HuffError, Result};

/// CRC of `data` over the given running CRC (`0xFFFF_FFFF` to start).
pub fn crc32_update(mut crc: u32, data: &[u8]) -> u32 {
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            // Branchless mask: `(crc & 1) * 0xEDB8_8320` under wrapping arithmetic.
            crc = (crc >> 1) ^ (0xEDB8_8320u32 & 0u32.wrapping_sub(crc & 1));
        }
    }
    crc
}

/// Standard CRC-32 of `data` (init all-ones, result XOR all-ones).
pub fn crc32(data: &[u8]) -> u32 {
    crc32_update(0xFFFF_FFFF, data) ^ 0xFFFF_FFFF
}

/// Return [`HuffError`] produced by the caller unless the stored CRC matches.
pub(crate) fn expect_crc(data: &[u8], stored: u32, mismatch: HuffError) -> Result<()> {
    if crc32(data) == stored {
        Ok(())
    } else {
        Err(mismatch)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksum_empty() {
        assert_eq!(crc32(b""), 0);
    }

    #[test]
    fn checksum_known_vectors() {
        // Golden values from the CRC-32/ISO-HDLC catalog.
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b"a"), 0xE8B7_BE43);
    }
}
