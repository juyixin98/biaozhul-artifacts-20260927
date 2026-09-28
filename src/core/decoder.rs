//! LZ77 decoder kernel.
//!
//! Two layers:
//! * [`decode_payload`] — pure, stateless payload decoding given the frame type
//!   and predecessor dictionary. The independent reference decompressor
//!   (`src/reference`) re-derives the same algorithm from scratch; both must
//!   agree byte-for-byte.
//! * [`ChainSession`] — stateful block-chain rules: index continuity,
//!   predecessor digest binding, dictionary rollover.
//!
//! Resource discipline: the output buffer is allocated only from the
//! *header-declared* length, which [`BlockHeader::decode`] has already bounded
//! by [`MAX_OUTPUT`] and by the expansion ratio. Every append additionally goes
//! through [`reserve_output`], which checks against the running budget before a
//! single byte is produced, so a stream whose real length exceeds the declared
//! length cannot grow memory beyond the cap.

use crate::core::checksum::dict_digest;
use crate::core::constants::{MAX_OUTPUT, WINDOW_SIZE};
use crate::core::error::{Code, Error, Result};
use crate::core::format::{BlockHeader, FrameType, Token, TokenParser};

/// Check that `current + extra` stays within the fixed per-block cap, *before*
/// the bytes exist. Returning an error here means no over-allocation path.
#[inline]
fn reserve_output(current: usize, extra: usize, declared: u64) -> Result<()> {
    let new_len = current
        .checked_add(extra)
        .ok_or_else(|| Error::new(Code::OutputCapExceeded, "output length arithmetic overflow"))?;
    if new_len > MAX_OUTPUT {
        return Err(Error::new(
            Code::OutputCapExceeded,
            format!("output would reach {new_len} bytes, cap {MAX_OUTPUT}"),
        ));
    }
    if new_len as u64 > declared {
        return Err(Error::new(
            Code::LengthMismatch,
            format!("tokens already produced {new_len} bytes, header declared {declared}"),
        ));
    }
    Ok(())
}

/// Decode a verified payload.
///
/// * `Independent`: `dict` must be empty.
/// * `Dependent`: `dict` is the predecessor window tail; matches may read it.
///
/// Returns the recovered bytes.
pub fn decode_payload(
    frame: FrameType,
    payload: &[u8],
    dict: &[u8],
    declared_len: u64,
) -> Result<Vec<u8>> {
    if frame == FrameType::Independent && !dict.is_empty() {
        return Err(Error::new(
            Code::DigestMismatch,
            "independent block cannot be decoded over a nonempty dictionary",
        ));
    }
    if dict.len() > WINDOW_SIZE {
        return Err(Error::new(
            Code::BadLength,
            format!(
                "dictionary {} bytes exceeds window {WINDOW_SIZE}",
                dict.len()
            ),
        ));
    }
    if declared_len > MAX_OUTPUT as u64 {
        return Err(Error::new(
            Code::OutputCapExceeded,
            format!("declared {declared_len} exceeds cap {MAX_OUTPUT}"),
        ));
    }

    // Allocate only against the header-validated declared length.
    let cap = declared_len as usize;
    let mut out: Vec<u8> = Vec::with_capacity(cap);

    // Working history = dict (read-only prefix) + produced output. We keep
    // output separate and index matches across the boundary via an offset,
    // rather than copying the dictionary into the allocation.
    let mut parser = TokenParser::new(payload);

    while let Some(token) = parser.next_token()? {
        match token {
            Token::Literal(data) => {
                reserve_output(out.len(), data.len(), declared_len)?;
                out.extend_from_slice(&data);
            }
            Token::Match { distance, length } => {
                // Fixed-format distance range: 1..=WINDOW_SIZE, independent of
                // how much history happens to exist.
                if distance > WINDOW_SIZE {
                    return Err(Error::new(
                        Code::BadDistance,
                        format!("match distance {distance} exceeds fixed window {WINDOW_SIZE}"),
                    ));
                }
                // Distance may reach into the dictionary prefix.
                let available = dict.len() + out.len();
                if distance > available {
                    return Err(Error::new(
                        Code::BadDistance,
                        format!(
                            "match distance {distance} points before history start ({available} bytes available)"
                        ),
                    ));
                }
                reserve_output(out.len(), length, declared_len)?;
                emit_overlapping(&mut out, dict, distance, length)?;
            }
            Token::End => {
                if !parser.is_finished() {
                    return Err(Error::new(Code::BadTokenStream, "bytes after END token"));
                }
                break;
            }
        }
    }

    if !parser.is_finished() {
        return Err(Error::new(
            Code::BadTokenStream,
            "payload ended before END token",
        ));
    }
    if out.len() as u64 != declared_len {
        return Err(Error::new(
            Code::LengthMismatch,
            format!(
                "produced {} bytes, header declared {declared_len}",
                out.len()
            ),
        ));
    }
    Ok(out)
}

/// Emit `length` bytes from a match at `distance` back.
///
/// Overlap semantics follow DEFLATE: when `length > distance`, the source
/// region overlaps bytes that do not exist yet. The correct behaviour is a
/// **byte-by-byte** copy, so the run reproduces the first `distance` bytes as a
/// periodic pattern. We therefore index a growing `[dict ++ out]` view per
/// produced byte — never a slice copy of bytes that are not there yet.
fn emit_overlapping(out: &mut Vec<u8>, dict: &[u8], distance: usize, length: usize) -> Result<()> {
    for _ in 0..length {
        // Source index in the virtual history, from its very start.
        let src = dict.len() + out.len() - distance;
        let byte = if src < dict.len() {
            dict[src]
        } else {
            // Reading `out` while extending it is safe: the index is always
            // below the current length (distance >= 1), and Vec::push after
            // this read cannot observe a dangling borrow.
            *out.get(src - dict.len()).ok_or_else(|| {
                Error::new(Code::BadDistance, "overlapping copy indexed beyond output")
            })?
        };
        out.push(byte);
    }
    Ok(())
}

/// Stateful decoder for one block chain. A chain resets whenever an
/// [`FrameType::Independent`] block is appended.
#[derive(Debug, Clone)]
pub struct ChainSession {
    /// 0 before the first block; afterwards, next expected index.
    next_index: u32,
    /// Dictionary that the *next* block will see (last WINDOW_SIZE bytes
    /// produced so far), empty at the start.
    dict: Vec<u8>,
    /// Digest bound by the last block (`digest(next_index_after_last, dict)`).
    last_digest: u64,
}

impl Default for ChainSession {
    fn default() -> Self {
        Self::new()
    }
}

impl ChainSession {
    pub fn new() -> Self {
        ChainSession {
            next_index: 0,
            dict: Vec::new(),
            last_digest: 0,
        }
    }

    pub fn next_index(&self) -> u32 {
        self.next_index
    }

    pub fn dict_len(&self) -> usize {
        self.dict.len()
    }

    /// Current dictionary bytes (the window tail the next dependent block
    /// binds against). Exposed for diagnostics and evidence tests.
    pub fn dict(&self) -> &[u8] {
        &self.dict
    }

    /// Digest the *current* dictionary expects at block `index`; exposed for
    /// tests/logging.
    pub fn expected_digest(&self, index: u32) -> u64 {
        dict_digest(index, &self.dict)
    }

    /// Fully decode one raw block through header validation, CRC, chain
    /// binding and payload expansion, then roll the dictionary forward.
    pub fn decode_raw(&mut self, raw: &[u8]) -> Result<Vec<u8>> {
        let header = BlockHeader::decode(raw)?;
        let payload = header.payload(raw);
        header.verify_crc(payload)?;

        if header.index != self.next_index {
            return Err(Error::new(
                Code::IndexGap,
                format!(
                    "block index {} out of order, expected {}",
                    header.index, self.next_index
                ),
            ));
        }

        match header.frame_type {
            FrameType::Independent => {
                if header.index != 0 {
                    // A mid-chain reset is structurally legal only if callers
                    // explicitly build a new session; within one session the
                    // binding rule forbids it so chains stay unambiguous.
                    return Err(Error::new(
                        Code::IndexGap,
                        "independent block is only valid as index 0 of a fresh chain",
                    ));
                }
                if header.prev_digest != 0 {
                    return Err(Error::new(
                        Code::DigestMismatch,
                        "independent block must bind digest 0",
                    ));
                }
            }
            FrameType::Dependent => {
                if header.index == 0 {
                    return Err(Error::new(
                        Code::DigestMismatch,
                        "block at index 0 cannot be dependent",
                    ));
                }
                let expected = self.expected_digest(header.index);
                if header.prev_digest != expected {
                    return Err(Error::new(
                        Code::DigestMismatch,
                        format!(
                            "block declares predecessor digest {:#018x}, chain expects {expected:#018x}",
                            header.prev_digest
                        ),
                    ));
                }
            }
        }

        // Decode against a snapshot dictionary so that a failure leaves the
        // session state untouched.
        let decoded = decode_payload(
            header.frame_type,
            payload,
            &self.dict,
            header.decompressed_len,
        )?;
        // u32 block index cannot advance past its last value.
        let next = header.index.checked_add(1).ok_or_else(|| {
            Error::new(
                Code::IndexGap,
                "block index overflow: chain is at its u32 limit",
            )
        })?;
        self.advance(next, &decoded);
        Ok(decoded)
    }

    fn advance(&mut self, next: u32, decoded: &[u8]) {
        // New dictionary = last WINDOW_SIZE bytes of (dict ++ decoded).
        let total = self.dict.len() + decoded.len();
        let take = total.min(WINDOW_SIZE);
        let mut next_dict = Vec::with_capacity(take);
        let skip = total - take;
        if skip < self.dict.len() {
            next_dict.extend_from_slice(&self.dict[skip..]);
            let already = self.dict.len() - skip;
            next_dict.extend_from_slice(&decoded[..take - already]);
        } else {
            let dskip = skip - self.dict.len();
            next_dict.extend_from_slice(&decoded[dskip..dskip + take]);
        }
        self.dict = next_dict;
        self.next_index = next;
        self.last_digest = dict_digest(self.next_index, &self.dict);
    }

    /// Digest the session would bind for a fresh dependent block right now.
    pub fn current_digest(&self) -> u64 {
        self.last_digest
    }
}

/// Encode the next block for this chain, choosing frame type and digest
/// automatically. Convenience used by the HTTP/store path; tests usually call
/// [`crate::core::encoder::encode_block`] directly to forge malformed inputs.
pub fn encode_next(
    session: &ChainSession,
    input: &[u8],
) -> Result<crate::core::encoder::EncodeOutcome> {
    let index = session.next_index;
    if index == 0 {
        crate::core::encoder::encode_block(FrameType::Independent, 0, &[], input)
    } else {
        crate::core::encoder::encode_block(FrameType::Dependent, index, &session.dict, input)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::constants::HEADER_LEN;
    use crate::core::encoder::encode_block;
    use crate::core::error::Category;

    #[test]
    fn overlap_copy_reproduces_periodic_pattern() {
        // Input crafted to force distance=1, long length: AAAA...
        let input = vec![b'A'; 50];
        let out = encode_block(FrameType::Independent, 0, &[], &input).unwrap();
        let h = BlockHeader::decode(&out.raw).unwrap();
        let got = decode_payload(FrameType::Independent, h.payload(&out.raw), &[], 50).unwrap();
        assert_eq!(got, input);
    }

    #[test]
    fn distance_beyond_history_is_input_error() {
        // Hand-built: literal "a", then match distance 9, len MIN_MATCH.
        let mut p = vec![0x00u8, 1, b'a', 0x01];
        crate::core::varint::write(&mut p, 9);
        crate::core::varint::write(&mut p, 0);
        p.push(0x02);
        let err = decode_payload(FrameType::Independent, &p, &[], 4).unwrap_err();
        assert_eq!(err.code, Code::BadDistance);
        assert_eq!(err.category(), Category::Input);
    }

    #[test]
    fn declared_length_underrun_is_input_error() {
        // One literal byte "a", but header claims 3.
        let p = vec![0x00, 1, b'a', 0x02];
        let err = decode_payload(FrameType::Independent, &p, &[], 3).unwrap_err();
        assert_eq!(err.code, Code::LengthMismatch);
    }

    #[test]
    fn chain_binds_digests_and_advances() {
        let mut s = ChainSession::new();
        let b0 = encode_next(&s, b"dictionary phrase repeated phrase").unwrap();
        let p0 = s.decode_raw(&b0.raw).unwrap();
        assert_eq!(p0, b"dictionary phrase repeated phrase");

        // Session parked after b0 (expecting index 1) for tamper experiments.
        let mut parked = s.clone();

        let b1 = encode_next(&s, b"phrase once more").unwrap();
        let p1 = s.decode_raw(&b1.raw).unwrap();
        assert_eq!(p1, b"phrase once more");
        assert_eq!(s.next_index, 2);

        // Tamper the declared digest while keeping payload bytes intact, and
        // present it as the block at index 1: chain binding must reject it.
        let mut tampered = b1.raw.clone();
        tampered[10..18].copy_from_slice(&0x1122_3344_5566_7788u64.to_be_bytes());
        let err = parked.decode_raw(&tampered).unwrap_err();
        assert_eq!(err.code, Code::DigestMismatch);

        // A payload bit flip is caught by the payload CRC before parsing.
        let mut crc_flip = b1.raw.clone();
        crc_flip[HEADER_LEN] ^= 0x80;
        assert_eq!(
            parked.decode_raw(&crc_flip).unwrap_err().code,
            Code::CrcMismatch
        );

        // Feed b1 to a fresh empty session: index 1 is out of order.
        let mut fresh = ChainSession::new();
        let err = fresh.decode_raw(&b1.raw).unwrap_err();
        assert_eq!(err.code, Code::IndexGap);

        // Feed b1 to a session that only has b0 but a different dictionary:
        // digest mismatch (the state-conflict class).
        let mut other = ChainSession::new();
        let foreign = encode_block(
            FrameType::Independent,
            0,
            &[],
            b"completely different content here!!",
        )
        .unwrap();
        other.decode_raw(&foreign.raw).unwrap();
        let err = other.decode_raw(&b1.raw).unwrap_err();
        assert_eq!(err.code, Code::DigestMismatch);
    }
}
