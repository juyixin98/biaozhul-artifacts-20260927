//! On-disk / over-the-wire data format: fixed constants, the block envelope and
//! integrity primitives (SHA-256 dictionary digest, CRC-32 frame check).
//!
//! # Wire layout (little-endian, one byte = one octet)
//!
//! ```text
//! offset  size  field
//! 0       4     magic        = b"LZ71" (ASCII; "LZ77 block, version 1")
//! 4       1     version      = 0x01
//! 5       1     flags        bit0: DEPENDENT
//! 6       32    dict_digest  SHA-256 of the decoder dictionary tail (independent: all zero)
//! 38      8     data_len     declared decompressed payload length of THIS block, u64 LE
//! 46      8     payload_len  encoded LZ77 stream length, u64 LE
//! 54      4     crc32        IEEE CRC-32 of `payload`, u32 LE
//! 58      ..    payload      LZ77 token stream (see `crate::lz77`)
//! ```
//!
//! The header is deliberately fixed-width and fully validated before any token is
//! decoded, so a malicious `data_len`/`payload_len` is rejected up front without any
//! allocation proportional to the declared values (see [`Envelope::check_limits`]).

use sha2::{Digest, Sha256};

use crate::error::{CodecError, Result};

/// Sliding dictionary window size in bytes. Distances are 1-based within the window.
pub const WINDOW_BYTES: usize = 4096;
/// Shortest match the encoder emits and the decoder accepts as a valid token.
pub const MIN_MATCH: usize = 3;
/// Longest encodable match (length field is one octet: `MIN_MATCH + 255`).
pub const MAX_MATCH: usize = 258;
/// Maximum decompressed bytes a single block may declare / produce.
pub const MAX_OUTPUT_BYTES: u64 = 8 * 1024 * 1024;
/// Maximum accepted `data_len / payload_len` ratio for a block. Random data compresses
/// to ~1.004x; any ratio this high is only achievable by a malicious stream.
pub const MAX_EXPANSION_RATIO: u64 = 100;
/// Maximum number of blocks followed while resolving a dependency chain.
pub const MAX_CHAIN_BLOCKS: usize = 64;

/// Token control octets.
pub const CTRL_LITERAL: u8 = 0x00;
pub const CTRL_MATCH: u8 = 0x01;
pub const CTRL_END: u8 = 0xFF;

pub const MAGIC: [u8; 4] = *b"LZ71";
pub const VERSION: u8 = 0x01;
/// flags bit 0: block depends on the previous block's output tail.
pub const FLAG_DEPENDENT: u8 = 0x01;

/// Fixed header length (everything before `payload`).
pub const HEADER_LEN: usize = 58;

/// Dependency mode. Independent blocks carry an all-zero digest and an empty dictionary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockMode {
    Independent,
    Dependent,
}

impl BlockMode {
    pub fn is_dependent(self) -> bool {
        matches!(self, BlockMode::Dependent)
    }

    pub fn flag_byte(self) -> u8 {
        match self {
            BlockMode::Independent => 0,
            BlockMode::Dependent => FLAG_DEPENDENT,
        }
    }

    pub fn from_flags(flags: u8) -> Result<Self> {
        if flags & !FLAG_DEPENDENT != 0 {
            return Err(CodecError::input(format!(
                "reserved flag bits set: 0x{flags:02x}"
            )));
        }
        Ok(if flags & FLAG_DEPENDENT != 0 {
            BlockMode::Dependent
        } else {
            BlockMode::Independent
        })
    }
}

/// Parsed envelope. The payload is borrowed from the original byte buffer; callers
/// keep that buffer alive while decoding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Envelope<'a> {
    pub mode: BlockMode,
    pub dict_digest: [u8; 32],
    pub data_len: u64,
    pub payload: &'a [u8],
}

/// Return the trailing [`WINDOW_BYTES`] of `data` (the whole slice when shorter).
/// The LZ77 kernel can only address the last `WINDOW_BYTES` of history, so any
/// accumulated multi-block dictionary must be trimmed to this tail before encoding
/// or decoding; digesting and binding operate on the same tail.
pub fn tail_window(data: &[u8]) -> &[u8] {
    let start = data.len().saturating_sub(WINDOW_BYTES);
    &data[start..]
}

/// SHA-256 digest of the last [`WINDOW_BYTES`] of the decoder dictionary
/// (the full dictionary when it is shorter than the window).
pub fn dictionary_digest(dictionary: &[u8]) -> [u8; 32] {
    let tail = tail_window(dictionary);
    let mut hasher = Sha256::new();
    hasher.update(tail);
    hasher.finalize().into()
}

/// Encode one complete block frame.
pub fn encode_frame(
    mode: BlockMode,
    dict_digest: &[u8; 32],
    data_len: u64,
    payload: &[u8],
) -> Vec<u8> {
    debug_assert!(
        mode.is_dependent() || dict_digest.iter().all(|b| *b == 0),
        "independent blocks must carry a zero digest"
    );
    let mut out = Vec::with_capacity(HEADER_LEN + payload.len());
    out.extend_from_slice(&MAGIC);
    out.push(VERSION);
    out.push(mode.flag_byte());
    out.extend_from_slice(dict_digest);
    out.extend_from_slice(&data_len.to_le_bytes());
    out.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    out.extend_from_slice(&crc32(payload).to_le_bytes());
    out.extend_from_slice(payload);
    out
}

/// Parse and fully validate a block frame header without decoding the payload.
/// Checks: length, magic, version, flags, declared lengths, expansion ratio, output cap,
/// and the payload CRC. Allocation failures caused by huge declared lengths are therefore
/// impossible: an over-large declaration is rejected here.
pub fn decode_frame(buf: &[u8]) -> Result<Envelope<'_>> {
    if buf.len() < HEADER_LEN {
        return Err(CodecError::input(format!(
            "frame too short: {0} bytes < header {HEADER_LEN}",
            buf.len()
        )));
    }
    if buf[0..4] != MAGIC {
        return Err(CodecError::input(format!(
            "bad magic: {:02x?} (want {:02x?})",
            &buf[0..4],
            MAGIC
        )));
    }
    if buf[4] != VERSION {
        return Err(CodecError::input(format!(
            "unsupported version 0x{:02x} (want 0x{VERSION:02x})",
            buf[4]
        )));
    }
    let mode = BlockMode::from_flags(buf[5])?;

    let mut digest = [0u8; 32];
    digest.copy_from_slice(&buf[6..38]);
    if !mode.is_dependent() && digest != [0u8; 32] {
        return Err(CodecError::input(
            "independent block carries a nonzero dictionary digest",
        ));
    }

    let data_len = u64::from_le_bytes(buf[38..46].try_into().unwrap());
    let payload_len = u64::from_le_bytes(buf[46..54].try_into().unwrap());
    let crc = u32::from_le_bytes(buf[54..58].try_into().unwrap());

    if payload_len == 0 {
        return Err(CodecError::input("payload length is zero"));
    }
    let actual_payload_len = buf.len() - HEADER_LEN;
    if payload_len as usize != actual_payload_len {
        return Err(CodecError::input(format!(
            "payload length mismatch: header declares {payload_len}, frame holds {actual_payload_len}"
        )));
    }
    let payload = &buf[HEADER_LEN..];
    if crc32(payload) != crc {
        return Err(CodecError::input(format!(
            "payload CRC mismatch: frame 0x{crc:08x}, computed 0x{:08x}",
            crc32(payload)
        )));
    }

    let envelope = Envelope {
        mode,
        dict_digest: digest,
        data_len,
        payload,
    };
    envelope.check_limits()?;
    Ok(envelope)
}

impl Envelope<'_> {
    /// Enforce the decompression safety policy against the *declared* lengths.
    pub fn check_limits(&self) -> Result<()> {
        if self.data_len > MAX_OUTPUT_BYTES {
            return Err(CodecError::exhausted(format!(
                "declared output {} bytes exceeds per-block cap {MAX_OUTPUT_BYTES}",
                self.data_len
            )));
        }
        // payload_len is nonzero (checked by the caller).
        let payload_len = self.payload.len() as u64;
        if self.data_len > payload_len.saturating_mul(MAX_EXPANSION_RATIO) {
            return Err(CodecError::exhausted(format!(
                "declared expansion {}->{} bytes exceeds ratio {MAX_EXPANSION_RATIO}x",
                payload_len, self.data_len
            )));
        }
        Ok(())
    }
}

/// Read two octets as a little-endian u16.
pub fn read_u16(bytes: &[u8]) -> u16 {
    u16::from_le_bytes([bytes[0], bytes[1]])
}

// -------------------------------------------------------------------------------------
// CRC-32 (IEEE 802.3, poly 0xEDB88320 reflected). Independent table-based implementation
// local to this crate so integrity checks do not depend on an external crate.
// -------------------------------------------------------------------------------------

const CRC_TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut crc = i as u32;
        let mut j = 0;
        while j < 8 {
            crc = if crc & 1 != 0 {
                0xEDB8_8320 ^ (crc >> 1)
            } else {
                crc >> 1
            };
            j += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
};

/// CRC-32/IEEE (the zlib/PNG polynomial, init `0xFFFF_FFFF`, final xor-out).
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        let index = ((crc ^ b as u32) & 0xFF) as usize;
        crc = CRC_TABLE[index] ^ (crc >> 8);
    }
    crc ^ 0xFFFF_FFFF
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_known_vectors() {
        // Check values from the CRC-32 catalogue (zlib/PNG variant).
        assert_eq!(crc32(b""), 0x0000_0000);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b"a"), 0xE8B7_BE43);
    }

    #[test]
    fn flags_roundtrip_and_reserved_bits() {
        assert_eq!(BlockMode::from_flags(0).unwrap(), BlockMode::Independent);
        assert_eq!(
            BlockMode::from_flags(FLAG_DEPENDENT).unwrap(),
            BlockMode::Dependent
        );
        assert!(BlockMode::from_flags(0x02).is_err());
        assert!(BlockMode::from_flags(0x80).is_err());
    }

    #[test]
    fn frame_roundtrip_and_corruption() {
        let payload = [CTRL_END];
        let frame = encode_frame(BlockMode::Independent, &[0u8; 32], 0, &payload);
        let env = decode_frame(&frame).unwrap();
        assert_eq!(env.data_len, 0);
        assert_eq!(env.payload, &[CTRL_END]);

        let mut bad = frame.clone();
        bad[HEADER_LEN] = CTRL_LITERAL; // break payload -> CRC failure
        assert_eq!(
            decode_frame(&bad).unwrap_err().category,
            crate::error::ErrorCategory::Input
        );

        let mut huge = frame.clone();
        huge[38..46].copy_from_slice(&u64::MAX.to_le_bytes());
        assert_eq!(
            decode_frame(&huge).unwrap_err().category,
            crate::error::ErrorCategory::ResourceExhausted
        );
    }
}
