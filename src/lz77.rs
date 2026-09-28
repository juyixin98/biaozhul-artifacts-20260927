//! LZ77 encoding/index kernel over a fixed sliding window.
//!
//! ## Token stream
//!
//! | octets                     | meaning                                         |
//! |----------------------------|-------------------------------------------------|
//! | [`CTRL_LITERAL`] `=0x00`, `b` | emit literal byte `b`                        |
//! | [`CTRL_MATCH`] `=0x01`, `d_lo d_hi len` | copy `len + `[`MIN_MATCH`]` bytes from `d = u16le(distance, 1-based)` bytes back |
//! | [`CTRL_END`] `=0xFF`       | end of stream (required, exactly once)          |
//!
//! ## Overlapping copies
//!
//! A match may be *self-overlapping*: `length > distance` is legal and produces a
//! repeating pattern (run-length style). Copying `work[src..src+len]` in one slice
//! operation would be wrong because the tail of that range does not exist before the
//! copy starts. The decoder therefore copies **one byte at a time**, reading from an
//! index that grows with the output. This is the canonical LZ77 semantics and is
//! locked down by the `self_overlap_*` tests.
//!
//! ## Index
//!
//! The encoder is a classic hash-chain (deflate-style) parser over a working buffer
//! prefixed with the block's dictionary:
//!
//! * 3-byte rolling hash into a 8192-slot head table;
//! * per-position `prev` chain, most-recent first;
//! * at most [`MAX_CHAIN`] candidates and only candidates within [`WINDOW_BYTES`];
//! * greedy longest-match parsing (matches between [`MIN_MATCH`] and [`MAX_MATCH`]).
//!
//! Determinism matters: the same input always produces the same bytes, which lets the
//! test suite compare chunking modes byte-for-byte.

use crate::error::{CodecError, Result};
use crate::format::{
    read_u16, CTRL_END, CTRL_LITERAL, CTRL_MATCH, MAX_MATCH, MIN_MATCH, WINDOW_BYTES,
};

/// Maximum hash-chain candidates examined at one position (parser effort bound).
const MAX_CHAIN: usize = 32;
const HASH_BITS: usize = 13;
const HASH_SIZE: usize = 1 << HASH_BITS; // 8192
const HASH_MASK: usize = HASH_SIZE - 1;
const NO_POS: u32 = u32::MAX;

#[inline]
fn hash3(at: &[u8]) -> usize {
    // 13-bit FNV-ish 3-byte hash; good enough distribution for repetitive fixtures.
    (((at[0] as usize)
        .wrapping_mul(0x1E35A7BD)
        .wrapping_add((at[1] as usize).wrapping_mul(0x63))
        .wrapping_add(at[2] as usize))
        & 0xFFFF_FFFF)
        & HASH_MASK
}

/// Compress `data` with `dictionary` preloaded as the decoder window.
///
/// `dictionary` must be the tail of the already-emitted byte stream and must not be
/// longer than [`WINDOW_BYTES`]; pass an empty slice for independent blocks.
/// The returned vector is the LZ77 payload (without the block envelope).
pub fn compress(data: &[u8], dictionary: &[u8]) -> Result<Vec<u8>> {
    if dictionary.len() > WINDOW_BYTES {
        return Err(CodecError::internal(format!(
            "encoder dictionary {} bytes exceeds window {WINDOW_BYTES}",
            dictionary.len()
        )));
    }

    // One working buffer, never mutated after construction: positions in
    // `[start, end)` are the bytes this block must describe.
    let start = dictionary.len();
    let mut work = Vec::with_capacity(start + data.len());
    work.extend_from_slice(dictionary);
    work.extend_from_slice(data);
    let end = work.len();

    let total = end as u64;
    let mut head = vec![NO_POS; HASH_SIZE];
    let mut prev = vec![NO_POS; work.len()];

    // Index dictionary positions first (oldest -> newest so heads end up newest).
    for p in 0..start.saturating_sub(2) {
        let h = hash3(&work[p..]);
        prev[p] = head[h];
        head[h] = p as u32;
    }

    let insert = |head: &mut [u32], prev: &mut [u32], pos: usize| {
        if pos + 2 < end {
            let h = hash3(&work[pos..]);
            prev[pos] = head[h];
            head[h] = pos as u32;
        }
    };

    let mut out = Vec::new();
    let mut pos = start;
    while pos < end {
        let mut best_len = 0usize;
        let mut best_dist = 0usize;

        if pos + MIN_MATCH <= end {
            let h = hash3(&work[pos..]);
            let mut cand = head[h] as usize;
            let mut chain_steps = 0;
            while cand != NO_POS as usize && chain_steps < MAX_CHAIN {
                let dist = pos - cand;
                if dist == 0 || dist > WINDOW_BYTES {
                    break; // chain is strictly older; once too old, stop
                }
                // Compare at best_len+1 first: skip candidates that cannot beat best.
                if work[cand + best_len] == work[pos + best_len] {
                    let limit = (end - pos).min(MAX_MATCH);
                    let mut len = 0;
                    while len < limit && work[cand + len] == work[pos + len] {
                        len += 1;
                    }
                    if len > best_len {
                        best_len = len;
                        best_dist = dist;
                        if len == (end - pos).min(MAX_MATCH) {
                            break; // cannot do better
                        }
                    }
                }
                cand = prev[cand] as usize;
                chain_steps += 1;
            }
        }

        if best_len >= MIN_MATCH {
            out.push(CTRL_MATCH);
            out.extend_from_slice(&(best_dist as u16).to_le_bytes());
            out.push((best_len - MIN_MATCH) as u8);
            // Every covered position becomes a future reference point.
            for p in pos..pos + best_len {
                insert(&mut head, &mut prev, p);
            }
            pos += best_len;
        } else {
            out.push(CTRL_LITERAL);
            out.push(work[pos]);
            insert(&mut head, &mut prev, pos);
            pos += 1;
        }
    }
    out.push(CTRL_END);
    debug_assert_eq!(total, work.len() as u64);
    Ok(out)
}

/// Decode an LZ77 payload produced by [`compress`] (or by the independent Python
/// reference, which uses the same wire format).
///
/// * `dictionary` — window bytes inherited from previous blocks (empty = independent);
/// * `declared_len` — the block's envelope `data_len`; output beyond it is rejected
///   *before* being written, so a corrupt stream can never force an over-allocation;
/// * returns exactly the block's own bytes (the dictionary prefix is not returned).
///
/// Errors:
///
/// * [`ErrorCategory::Input`] — truncated/unknown token, bad or out-of-window distance,
///   output length disagreeing with the declaration;
/// * [`ErrorCategory::ResourceExhausted`] — cannot happen from payload growth alone
///   (the envelope pre-checks bounds) but is returned if the running output would cross
///   the declared length: callers treat "more than declared" as malformed input.
///
/// [`ErrorCategory::Input`]: crate::error::ErrorCategory::Input
/// [`ErrorCategory::ResourceExhausted`]: crate::error::ErrorCategory::ResourceExhausted
pub fn decode(payload: &[u8], dictionary: &[u8], declared_len: u64) -> Result<Vec<u8>> {
    if dictionary.len() > WINDOW_BYTES {
        return Err(CodecError::internal(format!(
            "decoder dictionary {} bytes exceeds window {WINDOW_BYTES}",
            dictionary.len()
        )));
    }

    let declared = declared_len as usize; // u64->usize: envelope check caps at 8 MiB
                                          // NOTE: we deliberately do NOT pre-allocate `declared` bytes. The envelope has
                                          // already proved `declared <= 100 * payload_len` and `declared <= 8 MiB`, so the
                                          // worst-case growth is bounded regardless; growth stays amortized as tokens are
                                          // appended. Only the (already-trusted, <= WINDOW_BYTES) dictionary is reserved.
    let mut work: Vec<u8> = Vec::with_capacity(dictionary.len());
    work.extend_from_slice(dictionary);
    let block_start = dictionary.len();

    let need = |cur: &mut usize, n: usize| -> Result<()> {
        if *cur + n > payload.len() {
            Err(CodecError::input(format!(
                "truncated stream at offset {}: need {n} octets, have {}",
                *cur,
                payload.len() - *cur
            )))
        } else {
            Ok(())
        }
    };

    let mut cur = 0usize;
    loop {
        need(&mut cur, 1)?;
        let ctrl = payload[cur];
        cur += 1;
        match ctrl {
            CTRL_LITERAL => {
                need(&mut cur, 1)?;
                if work.len() - block_start >= declared {
                    return Err(CodecError::input(
                        "stream produces more bytes than data_len declares",
                    ));
                }
                work.push(payload[cur]);
                cur += 1;
            }
            CTRL_MATCH => {
                need(&mut cur, 3)?;
                let distance = read_u16(&payload[cur..cur + 2]) as usize;
                let length = payload[cur + 2] as usize + MIN_MATCH;
                cur += 3;

                if distance == 0 {
                    return Err(CodecError::input("match with distance 0"));
                }
                if distance > WINDOW_BYTES {
                    return Err(CodecError::input(format!(
                        "match distance {distance} exceeds fixed window {WINDOW_BYTES}"
                    )));
                }
                if distance > work.len() {
                    return Err(CodecError::input(format!(
                        "match distance {distance} points before start of stream \
                         (available history {} bytes)",
                        work.len()
                    )));
                }
                if work.len() - block_start + length > declared {
                    return Err(CodecError::input(format!(
                        "match of {length} bytes would overrun declared data_len {declared}"
                    )));
                }

                // Byte-at-a-time copy: self-overlapping matches (length > distance)
                // read bytes that earlier iterations of this loop just wrote.
                let src = work.len() - distance;
                for k in 0..length {
                    // Indexed (not sliced) on purpose; see module docs.
                    let b = work[src + k];
                    work.push(b);
                }
            }
            CTRL_END => {
                if cur != payload.len() {
                    return Err(CodecError::input(format!(
                        "{0} trailing octets after end-of-stream marker",
                        payload.len() - cur
                    )));
                }
                let produced = work.len() - block_start;
                if produced != declared {
                    return Err(CodecError::input(format!(
                        "stream produced {produced} bytes but data_len declares {declared}"
                    )));
                }
                break;
            }
            other => {
                return Err(CodecError::input(format!(
                    "unknown control byte 0x{other:02x} at offset {}",
                    cur - 1
                )));
            }
        }
    }

    // Return only this block's bytes; the dictionary tail is managed by the codec layer.
    work.drain(..block_start);
    Ok(work)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorCategory;

    fn roundtrip(data: &[u8], dict: &[u8]) {
        let payload = compress(data, dict).unwrap();
        let got = decode(&payload, dict, data.len() as u64).unwrap();
        assert_eq!(got, data);
    }

    #[test]
    fn empty_and_literals() {
        roundtrip(b"", b"");
        roundtrip(b"a", b"");
        roundtrip(b"abc", b"");
    }

    #[test]
    fn self_overlap_rle() {
        // History "ab", then distance 1, length 9: each iteration copies the byte
        // the previous iteration just wrote ('b'), producing a run of 'b's.
        let mut payload = vec![CTRL_LITERAL, b'a', CTRL_LITERAL, b'b', CTRL_MATCH];
        payload.extend_from_slice(&1u16.to_le_bytes());
        payload.push(9 - MIN_MATCH as u8);
        payload.push(CTRL_END);
        let out = decode(&payload, b"", 11).unwrap();
        assert_eq!(out, b"abbbbbbbbbb");
    }

    #[test]
    fn self_overlap_longer_period() {
        // distance 3, length 8 with history "abc": abc + 8 copied = abcabcabcab (11).
        let mut payload = Vec::new();
        for b in b"abc" {
            payload.push(CTRL_LITERAL);
            payload.push(*b);
        }
        payload.push(CTRL_MATCH);
        payload.extend_from_slice(&3u16.to_le_bytes());
        payload.push(8 - MIN_MATCH as u8);
        payload.push(CTRL_END);
        let out = decode(&payload, b"", 11).unwrap();
        assert_eq!(out, b"abcabcabcab");
    }

    #[test]
    fn encoder_compresses_repetition() {
        let data = b"abcabcabcabcabc";
        let payload = compress(data, b"").unwrap();
        assert!(payload.len() < data.len());
        assert_eq!(decode(&payload, b"", data.len() as u64).unwrap(), data);
    }

    #[test]
    fn bad_distance_before_stream() {
        // distance 10 with no history at all.
        let payload = [CTRL_MATCH, 10, 0, 0, CTRL_END];
        let err = decode(&payload, b"", 3).unwrap_err();
        assert_eq!(err.category, ErrorCategory::Input);
    }

    #[test]
    fn distance_beyond_window() {
        // distance 4097 is never representable against history and must be rejected.
        let payload = [CTRL_MATCH, 0x01, 0x10, 0, CTRL_END];
        let err = decode(&payload, b"", 3).unwrap_err();
        assert_eq!(err.category, ErrorCategory::Input);
        assert!(err.detail.contains("exceeds fixed window"));
    }

    #[test]
    fn dictionary_references_roundtrip() {
        let dict = b"the quick brown fox ";
        let data = b"the quick brown fox jumps";
        roundtrip(data, dict);
    }
}
