//! Block-level codec: binds [`lz77`](crate::lz77) payloads to the
//! [`format`](crate::format) envelope and enforces the dependency contract.
//!
//! Dependency contract
//! -------------------
//! A *dependent* block's header carries the SHA-256 digest of the dictionary its
//! encoder saw (the trailing window of all preceding output). Decoding a dependent
//! block requires the caller to present the **same** dictionary; a mismatch is a
//! [`ErrorCategory::StateConflict`], not an input error, because the wire data itself
//! is well-formed — the receiver's state is wrong (missing/stale previous blocks).
//!
//! [`ErrorCategory::StateConflict`]: crate::error::ErrorCategory::StateConflict

use crate::error::{CodecError, Result};
use crate::format::{
    crc32, decode_frame, dictionary_digest, encode_frame, tail_window, BlockMode, Envelope,
    MAX_CHAIN_BLOCKS,
};
use crate::lz77;

/// Encode an independent block (empty dictionary, zero digest).
pub fn encode_independent(data: &[u8]) -> Vec<u8> {
    let payload = lz77::compress(data, b"").expect("empty dictionary is always valid");
    encode_frame(
        BlockMode::Independent,
        &[0u8; 32],
        data.len() as u64,
        &payload,
    )
}

/// Encode a dependent block bound to `dictionary` (the trailing window bytes of all
/// preceding blocks' output). Bytes older than the trailing
/// [`WINDOW_BYTES`](crate::format::WINDOW_BYTES) are unreachable and silently trimmed.
pub fn encode_dependent(data: &[u8], dictionary: &[u8]) -> Result<Vec<u8>> {
    let window = tail_window(dictionary);
    let payload = lz77::compress(data, window)?;
    let digest = dictionary_digest(window);
    Ok(encode_frame(
        BlockMode::Dependent,
        &digest,
        data.len() as u64,
        &payload,
    ))
}

/// Decode one block given the dictionary already assembled from its predecessors
/// (empty slice for an independent block).
///
/// Validates, in order: frame envelope (magic/CRC/lengths), mode/digest binding,
/// then the token stream. Returns the block's own decompressed bytes.
pub fn decode_one(frame: &[u8], dictionary: &[u8]) -> Result<Vec<u8>> {
    let env = decode_frame(frame)?;
    check_binding(&env, dictionary)?;
    lz77::decode(env.payload, tail_window(dictionary), env.data_len)
}

/// Verify that a parsed envelope matches the receiver's current dictionary state.
pub fn check_binding(env: &Envelope<'_>, dictionary: &[u8]) -> Result<()> {
    match env.mode {
        BlockMode::Independent => {
            if !dictionary.is_empty() {
                return Err(CodecError::state(format!(
                    "independent block cannot be decoded against a {} byte predecessor dictionary",
                    dictionary.len()
                )));
            }
            Ok(())
        }
        BlockMode::Dependent => {
            let want = dictionary_digest(dictionary);
            if want != env.dict_digest {
                return Err(CodecError::state(format!(
                    "dictionary digest mismatch: block expects {}, receiver has {}",
                    hex32(&env.dict_digest),
                    hex32(&want)
                )));
            }
            Ok(())
        }
    }
}

/// Decode an ordered, root-first chain of frames (the result of walking `prev_id`
/// links). Each frame must agree with the mode its position implies:
///
/// * the first frame must be independent;
/// * every subsequent frame must be dependent, and its digest must bind to the
///   trailing window of the combined output so far.
///
/// Returns the concatenation of every block's bytes — used by the chunking-mode
/// equivalence tests and by the `GET .../raw` route.
pub fn decode_chain(frames: &[Vec<u8>]) -> Result<Vec<u8>> {
    if frames.is_empty() {
        return Err(CodecError::input("empty block chain"));
    }
    if frames.len() > MAX_CHAIN_BLOCKS {
        return Err(CodecError::exhausted(format!(
            "dependency chain of {} blocks exceeds depth limit {MAX_CHAIN_BLOCKS}",
            frames.len()
        )));
    }

    let mut combined: Vec<u8> = Vec::new();
    for (index, frame) in frames.iter().enumerate() {
        let env = decode_frame(frame)?;
        match (index, env.mode) {
            (0, BlockMode::Dependent) => {
                return Err(CodecError::state(
                    "chain starts with a dependent block (no predecessor available)",
                ));
            }
            (i, BlockMode::Independent) if i > 0 => {
                return Err(CodecError::state(format!(
                    "independent block at chain position {i} (only the root may be independent)"
                )));
            }
            _ => check_binding(&env, &combined)?,
        }
        let window = tail_window(&combined);
        let block = lz77::decode(env.payload, window, env.data_len)?;
        combined.extend_from_slice(&block);
    }
    Ok(combined)
}

/// Result of the non-storage validation endpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationReport {
    pub valid: bool,
    pub mode: Option<BlockMode>,
    pub data_len: Option<u64>,
    pub payload_len: Option<u64>,
    pub crc32: Option<u32>,
    pub digest_hex: Option<String>,
    pub error_category: Option<String>,
    pub reason: String,
}

/// Parse + bind-check + fully decode a frame without persisting anything.
/// `dictionary` lets callers present a predecessor dictionary (empty for an
/// independent frame); only its trailing window participates in binding/decoding.
pub fn validate(frame: &[u8], dictionary: &[u8]) -> ValidationReport {
    let window = tail_window(dictionary);
    let run = (|| -> Result<(Envelope<'_>, Vec<u8>)> {
        let env = decode_frame(frame)?;
        check_binding(&env, window)?;
        let out = lz77::decode(env.payload, window, env.data_len)?;
        Ok((env, out))
    })();

    match run {
        Ok((env, out)) => ValidationReport {
            valid: true,
            mode: Some(env.mode),
            data_len: Some(env.data_len),
            payload_len: Some(env.payload.len() as u64),
            crc32: Some(crc32(env.payload)),
            digest_hex: Some(hex32(&env.dict_digest)),
            error_category: None,
            reason: format!("decoded {} bytes and passed all binding checks", out.len()),
        },
        Err(e) => ValidationReport {
            valid: false,
            mode: None,
            data_len: None,
            payload_len: None,
            crc32: None,
            digest_hex: None,
            error_category: Some(e.category.as_ref().to_string()),
            reason: e.detail,
        },
    }
}

fn hex32(bytes: &[u8; 32]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(64);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xF) as usize] as char);
    }
    s
}

/// Lowercase hex encoding for arbitrary digest/byte slices (fixture tooling).
pub fn hex_encode(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xF) as usize] as char);
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorCategory;

    #[test]
    fn independent_roundtrip() {
        let data = b"hello hello hello hello";
        let frame = encode_independent(data);
        assert_eq!(decode_one(&frame, b"").unwrap(), data);
    }

    #[test]
    fn dependent_digest_binding_roundtrip() {
        let prev = b"dictionary prefix dictionary prefix";
        let frame = encode_dependent(b"dictionary suffix", prev).unwrap();
        assert_eq!(decode_one(&frame, prev).unwrap(), b"dictionary suffix");
    }

    #[test]
    fn dependent_with_wrong_dictionary_is_state_conflict() {
        let frame = encode_dependent(b"data", b"correct history").unwrap();
        let err = decode_one(&frame, b"wrong history!!").unwrap_err();
        assert_eq!(err.category, ErrorCategory::StateConflict);
        assert!(err.detail.contains("digest mismatch"));
    }

    #[test]
    fn independent_decoded_with_dictionary_is_state_conflict() {
        let frame = encode_independent(b"data");
        let err = decode_one(&frame, b"x").unwrap_err();
        assert_eq!(err.category, ErrorCategory::StateConflict);
    }

    #[test]
    fn chain_modes_enforced() {
        let root = encode_independent(b"root ");
        let dep = encode_dependent(b"child", b"root ").unwrap();

        // Dependent first -> StateConflict (missing predecessor).
        let err = decode_chain(std::slice::from_ref(&dep)).unwrap_err();
        assert_eq!(err.category, ErrorCategory::StateConflict);

        // Independent second -> StateConflict.
        let second_root = encode_independent(b"other");
        let err = decode_chain(&[root.clone(), second_root]).unwrap_err();
        assert_eq!(err.category, ErrorCategory::StateConflict);

        assert_eq!(decode_chain(&[root, dep]).unwrap(), b"root child");
    }
}
