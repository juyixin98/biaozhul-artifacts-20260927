//! LZ77 encoder kernel.
//!
//! Greedy hash-chain compressor over a *combined* buffer of the predecessor
//! dictionary (possibly empty) followed by the new input. Matches may start in
//! the dictionary, so dependent blocks reference bytes from previous blocks.
//!
//! Nothing here trusts a length from outside: input size is capped before any
//! allocation, and a payload that would exceed the fixed [`MAX_PAYLOAD`] cap is
//! rejected with a resource error rather than silently truncated.

use crate::core::checksum::{crc32, dict_digest};
use crate::core::constants::{
    HASH_SIZE, MAX_CHAIN, MAX_ENCODE_INPUT, MAX_MATCH, MAX_PAYLOAD, MIN_MATCH, WINDOW_SIZE,
};
use crate::core::error::{Code, Error, Result};
use crate::core::format::{BlockHeader, FrameType};
use crate::core::varint;

const NIL: u32 = u32::MAX;

/// Facts about a produced block, used by logs and tests as "key intermediate state".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncodeStats {
    pub frame_type: FrameType,
    pub index: u32,
    pub prev_digest: u64,
    pub input_len: usize,
    pub dict_before_len: usize,
    pub dict_after_len: usize,
    pub literal_bytes: usize,
    pub match_tokens: usize,
    pub matched_bytes: usize,
    pub payload_len: usize,
}

#[derive(Debug, Clone)]
pub struct EncodeOutcome {
    /// Full block bytes (header + payload), ready to persist.
    pub raw: Vec<u8>,
    pub stats: EncodeStats,
}

/// Encode one block.
///
/// * `frame == Independent`: `dict` must be empty; the block carries digest 0.
/// * `frame == Dependent`: `dict` is the predecessor window tail (may be empty
///   only for an empty predecessor); the chaining digest is computed here from
///   `(index, dict)` — callers cannot invent it.
pub fn encode_block(
    frame: FrameType,
    index: u32,
    dict: &[u8],
    input: &[u8],
) -> Result<EncodeOutcome> {
    if input.len() > MAX_ENCODE_INPUT {
        return Err(Error::new(
            Code::OutputCapExceeded,
            format!(
                "input {} bytes exceeds per-block cap {MAX_ENCODE_INPUT}",
                input.len()
            ),
        ));
    }
    match frame {
        FrameType::Independent => {
            if !dict.is_empty() {
                return Err(Error::new(
                    Code::DigestMismatch,
                    "independent block cannot be encoded over a nonempty dictionary",
                ));
            }
        }
        FrameType::Dependent => {
            if dict.len() > WINDOW_SIZE {
                return Err(Error::new(
                    Code::BadLength,
                    format!(
                        "dictionary {} bytes exceeds window {WINDOW_SIZE}",
                        dict.len()
                    ),
                ));
            }
        }
    }

    // Combined search arena: dictionary prefix + fresh input.
    let mut data: Vec<u8> = Vec::with_capacity(dict.len() + input.len());
    data.extend_from_slice(dict);
    data.extend_from_slice(input);
    let dict_len = dict.len();
    let n = data.len();

    let mut head = vec![NIL; HASH_SIZE];
    let mut prev = vec![NIL; n];

    let payload = greedy_parse(&data, dict_len, n, &mut head, &mut prev)?;

    if payload.len() > MAX_PAYLOAD {
        return Err(Error::new(
            Code::PayloadCapExceeded,
            format!(
                "encoded payload {} bytes exceeds cap {MAX_PAYLOAD}",
                payload.len()
            ),
        ));
    }

    let prev_digest = match frame {
        FrameType::Independent => 0,
        FrameType::Dependent => dict_digest(index, dict),
    };

    let mut raw = Vec::with_capacity(crate::core::constants::HEADER_LEN + payload.len());
    BlockHeader {
        frame_type: frame,
        index,
        prev_digest,
        payload_crc: crc32(&payload),
        decompressed_len: input.len() as u64,
    }
    .encode(&mut raw);
    raw.extend_from_slice(&payload);

    let dict_after_len = n.min(WINDOW_SIZE);
    let stats = collect_stats(&payload)?;
    Ok(EncodeOutcome {
        raw,
        stats: EncodeStats {
            frame_type: frame,
            index,
            prev_digest,
            input_len: input.len(),
            dict_before_len: dict_len,
            dict_after_len,
            literal_bytes: stats.literal_bytes,
            match_tokens: stats.match_tokens,
            matched_bytes: stats.matched_bytes,
            payload_len: payload.len(),
        },
    })
}

#[derive(Debug, Default)]
struct RawStats {
    literal_bytes: usize,
    match_tokens: usize,
    matched_bytes: usize,
}

fn collect_stats(payload: &[u8]) -> Result<RawStats> {
    // Recount from the finished payload so reported numbers describe bytes
    // actually on the wire, not encoder bookkeeping.
    let mut parser = crate::core::format::TokenParser::new(payload);
    let mut s = RawStats::default();
    while let Some(tok) = parser.next_token()? {
        match tok {
            crate::core::format::Token::Literal(v) => s.literal_bytes += v.len(),
            crate::core::format::Token::Match { length, .. } => {
                s.match_tokens += 1;
                s.matched_bytes += length;
            }
            crate::core::format::Token::End => break,
        }
    }
    if !parser.is_finished() {
        return Err(Error::new(
            Code::BadTokenStream,
            "encoder produced a payload without END",
        ));
    }
    Ok(s)
}

#[inline]
fn hash3(data: &[u8], p: usize) -> usize {
    // 3-byte rolling-ish hash (FNV-like); only needs to distribute triples.
    let h = (u32::from(data[p]) << 10) ^ (u32::from(data[p + 1]) << 5) ^ u32::from(data[p + 2]);
    (h as usize) & (HASH_SIZE - 1)
}

/// Greedy parse. Only positions `>= start` (the input region) are emitted.
#[allow(clippy::needless_range_loop)] // indices coordinate head[]/prev[]/data[]
fn greedy_parse(
    data: &[u8],
    start: usize,
    n: usize,
    head: &mut [u32],
    prev: &mut [u32],
) -> Result<Vec<u8>> {
    let mut payload: Vec<u8> = Vec::new();
    let mut pending_literal: Vec<u8> = Vec::new();
    let mut p = start;

    // Prime chains with every dictionary position that starts a triple.
    // Positions `start-2 .. start-1` that straddle the boundary are inserted
    // once p reaches/passes them below (insertion looks up triples at q, so
    // q..q+3 must be inside `data`, which it is once q <= start).
    for q in 0..start.saturating_sub(MIN_MATCH - 1) {
        let h = hash3(data, q);
        prev[q] = head[h];
        head[h] = q as u32;
    }
    // Straddling triples that begin in the last MIN_MATCH-1 dictionary bytes
    // but include input bytes.
    for q in start.saturating_sub(MIN_MATCH - 1)..start {
        if q + MIN_MATCH <= n {
            let h = hash3(data, q);
            prev[q] = head[h];
            head[h] = q as u32;
        }
    }

    while p < n {
        let mut best_len = 0usize;
        let mut best_dist = 0usize;

        if p + MIN_MATCH <= n {
            let h = hash3(data, p);
            let mut candidate = head[h] as usize;
            let mut chain = 0usize;
            while candidate != NIL as usize && chain < MAX_CHAIN {
                chain += 1;
                // Only strictly earlier positions may be referenced; straddle
                // priming can leave entries that point at/after p in the head.
                if candidate >= p {
                    break;
                }
                // Distance window: candidate must be within WINDOW_SIZE behind p.
                if p - candidate > WINDOW_SIZE {
                    break;
                }
                let max_len = (n - p).min(MAX_MATCH);
                let len = common_prefix(data, candidate, p, max_len);
                if len > best_len {
                    best_len = len;
                    best_dist = p - candidate;
                    if len == max_len {
                        break;
                    }
                }
                let pr = prev[candidate] as usize;
                if pr == NIL as usize || pr >= candidate {
                    break;
                }
                candidate = pr;
            }
            // Insert the head position for p before advancing.
            prev[p] = head[h];
            head[h] = p as u32;
        }

        if best_len >= MIN_MATCH {
            flush_literals(&mut payload, &mut pending_literal);
            payload.push(0x01);
            varint::write(&mut payload, best_dist as u64);
            varint::write(&mut payload, (best_len - MIN_MATCH) as u64);
            // Insert all skipped positions (except p already inserted) so the
            // dictionary sees the inside of the match — standard "nice" behaviour.
            for q in p + 1..p + best_len {
                if q + MIN_MATCH <= n {
                    let hq = hash3(data, q);
                    prev[q] = head[hq];
                    head[hq] = q as u32;
                }
            }
            p += best_len;
        } else {
            pending_literal.push(data[p]);
            p += 1;
        }
    }

    flush_literals(&mut payload, &mut pending_literal);
    payload.push(0x02);
    Ok(payload)
}

fn flush_literals(payload: &mut Vec<u8>, pending: &mut Vec<u8>) {
    if pending.is_empty() {
        return;
    }
    payload.push(0x00);
    varint::write(payload, pending.len() as u64);
    payload.append(pending);
}

#[inline]
fn common_prefix(data: &[u8], a: usize, b: usize, max_len: usize) -> usize {
    let mut i = 0;
    // Byte-wise compare; positions may cross the dict/input boundary but both
    // are inside `data`, so this is always valid memory.
    while i < max_len && data[a + i] == data[b + i] {
        i += 1;
    }
    i
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::decoder::decode_payload;
    use crate::core::format::BlockHeader;

    fn roundtrip(frame: FrameType, index: u32, dict: &[u8], input: &[u8]) -> EncodeOutcome {
        let out = encode_block(frame, index, dict, input).unwrap();
        let h = BlockHeader::decode(&out.raw).unwrap();
        assert_eq!(h.decompressed_len, input.len() as u64);
        let payload = h.payload(&out.raw);
        h.verify_crc(payload).unwrap();
        let dec = decode_payload(frame, payload, dict, h.decompressed_len).unwrap();
        assert_eq!(dec, input);
        out
    }

    #[test]
    fn compresses_independent_and_dependent() {
        let input = b"the rain in spain falls mainly on the plain, the rain in spain".repeat(4);
        let out = roundtrip(FrameType::Independent, 0, &[], &input);
        assert!(out.stats.match_tokens > 0);
        assert!(out.stats.payload_len < input.len());

        // Dependent block that finds the repeated phrase only across boundary.
        let next = b"the rain in spain again and again";
        let tail = input.len().min(WINDOW_SIZE);
        let dict: Vec<u8> = input[input.len() - tail..].to_vec();
        let out = roundtrip(FrameType::Dependent, 1, &dict, next);
        assert!(
            out.stats.match_tokens > 0,
            "should reuse predecessor dictionary"
        );
    }

    #[test]
    fn incompressible_input_is_rejected_when_payload_too_big() {
        // Deterministic LCG pseudo-random stream: no repeats long enough to
        // match, so the payload (literals) necessarily exceeds the 64 KiB cap
        // for a full 1 MiB input.
        let mut state = 0x1234_5678u32;
        let input: Vec<u8> = (0..MAX_ENCODE_INPUT)
            .map(|_| {
                // Numerical Recipes constants; xorshift mixing.
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                (state & 0xff) as u8
            })
            .collect();
        let err = encode_block(FrameType::Independent, 0, &[], &input).unwrap_err();
        assert_eq!(err.code, Code::PayloadCapExceeded);
        assert_eq!(err.category(), crate::core::error::Category::Resource);
    }

    #[test]
    fn independent_with_dict_is_state_error() {
        let err = encode_block(FrameType::Independent, 0, b"abc", b"x").unwrap_err();
        assert_eq!(err.category(), crate::core::error::Category::State);
    }
}
