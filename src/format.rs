//! On-disk index format (little-endian).
//!
//! The header binds the build seed and the algorithm/format versions:
//! a loader refuses anything whose versions it does not implement, and a
//! CRC over the payload catches torn or tampered files.
//!
//! ```text
//! offset  size  field
//! 0       8     magic "MPHFBDZ\x01"
//! 8       2     format_version   (= 1)
//! 10      2     algorithm_version(= 1)
//! 12      4     reserved (0)
//! 16      8     seed
//! 24      8     n (key count)
//! 32      8     m (vertex count)
//! 40      8     payload_len (bytes)
//! 48      8     payload_crc (keyed64 over header[0..48] ++ payload)
//! 56      ..    payload: g[m] as u32 LE, then fps[n] as u64 LE
//! ```
//!
//! The CRC therefore covers the seed and version fields too: flipping
//! any header byte makes the file unverifiable.

use crate::hash::keyed64;
use crate::kernel::MphIndex;

pub const MAGIC: &[u8; 8] = b"MPHFBDZ\x01";
pub const FORMAT_VERSION: u16 = 1;
pub const ALGORITHM_VERSION: u16 = 1;
pub const HEADER_LEN: usize = 56;
const CRC_BASIS: u64 = 0xCBF2_9CE4_8422_2325;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum FormatError {
    #[error("bad magic bytes")]
    BadMagic,
    #[error("unsupported format version {0} (supported: {FORMAT_VERSION})")]
    UnsupportedFormatVersion(u16),
    #[error("unsupported algorithm version {0} (supported: {ALGORITHM_VERSION})")]
    UnsupportedAlgorithmVersion(u16),
    #[error("file truncated: need {need} bytes, have {have}")]
    Truncated { need: usize, have: usize },
    #[error("payload length mismatch: header says {declared}, file has {actual}")]
    LengthMismatch { declared: u64, actual: u64 },
    #[error("payload CRC mismatch")]
    CrcMismatch,
    #[error("inconsistent counts: n={n}, m={m} cannot describe a valid index")]
    InconsistentCounts { n: u64, m: u64 },
}

/// CRC over the fixed header prefix (including seed/versions) followed
/// by the payload.
pub fn crc_of(header_prefix: &[u8], payload: &[u8]) -> u64 {
    let mut data = Vec::with_capacity(header_prefix.len() + payload.len());
    data.extend_from_slice(header_prefix);
    data.extend_from_slice(payload);
    keyed64(&data, CRC_BASIS)
}

pub fn encode(index: &MphIndex) -> Vec<u8> {
    let mut payload = Vec::with_capacity(index.m * 4 + index.n * 8);
    for &v in &index.g {
        payload.extend_from_slice(&v.to_le_bytes());
    }
    for &f in &index.fps {
        payload.extend_from_slice(&f.to_le_bytes());
    }

    let mut out = Vec::with_capacity(HEADER_LEN + payload.len());
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    out.extend_from_slice(&ALGORITHM_VERSION.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&index.seed.to_le_bytes());
    out.extend_from_slice(&(index.n as u64).to_le_bytes());
    out.extend_from_slice(&(index.m as u64).to_le_bytes());
    out.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    let crc = crc_of(&out[..48], &payload);
    out.extend_from_slice(&crc.to_le_bytes());
    out.extend_from_slice(&payload);
    out
}

pub fn decode(bytes: &[u8]) -> Result<MphIndex, FormatError> {
    if bytes.len() < HEADER_LEN {
        return Err(FormatError::Truncated {
            need: HEADER_LEN,
            have: bytes.len(),
        });
    }
    if &bytes[0..8] != MAGIC {
        return Err(FormatError::BadMagic);
    }
    let fmt_ver = u16::from_le_bytes(bytes[8..10].try_into().unwrap());
    let algo_ver = u16::from_le_bytes(bytes[10..12].try_into().unwrap());
    if fmt_ver != FORMAT_VERSION {
        return Err(FormatError::UnsupportedFormatVersion(fmt_ver));
    }
    if algo_ver != ALGORITHM_VERSION {
        return Err(FormatError::UnsupportedAlgorithmVersion(algo_ver));
    }
    let seed = u64::from_le_bytes(bytes[16..24].try_into().unwrap());
    let n = u64::from_le_bytes(bytes[24..32].try_into().unwrap());
    let m = u64::from_le_bytes(bytes[32..40].try_into().unwrap());
    let payload_len = u64::from_le_bytes(bytes[40..48].try_into().unwrap());
    let crc = u64::from_le_bytes(bytes[48..56].try_into().unwrap());

    let expected = m
        .checked_mul(4)
        .and_then(|g| n.checked_mul(8).and_then(|f| g.checked_add(f)))
        .ok_or(FormatError::InconsistentCounts { n, m })?;
    if payload_len != expected {
        return Err(FormatError::InconsistentCounts { n, m });
    }
    let actual = (bytes.len() - HEADER_LEN) as u64;
    if actual != payload_len {
        return Err(FormatError::LengthMismatch {
            declared: payload_len,
            actual,
        });
    }
    let payload = &bytes[HEADER_LEN..];
    if crc_of(&bytes[..48], payload) != crc {
        return Err(FormatError::CrcMismatch);
    }

    let m = m as usize;
    let n = n as usize;
    let mut g = Vec::with_capacity(m);
    let mut off = 0;
    for _ in 0..m {
        g.push(u32::from_le_bytes(payload[off..off + 4].try_into().unwrap()));
        off += 4;
    }
    let mut fps = Vec::with_capacity(n);
    for _ in 0..n {
        fps.push(u64::from_le_bytes(payload[off..off + 8].try_into().unwrap()));
        off += 8;
    }
    Ok(MphIndex::new(seed, n, m, g, fps))
}
