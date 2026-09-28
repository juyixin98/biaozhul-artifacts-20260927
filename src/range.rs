//! Fixed-precision range-coding kernel (32-bit, byte-at-a-time, big-endian).
//!
//! This is an integer-only range coder in the style of Subbotin/Pavlov:
//! every quantity is an exact integer; there are no floating-point
//! probabilities.  Encoder and decoder perform byte-for-byte the same
//! integer divisions, so they agree by construction on every interval
//! boundary.
//!
//! # State and invariants
//!
//! ```text
//!   0 <= low  < 2**33          // kept in a u64; bit 32 is the carry bit
//!   2**24 <= range <= 2**32 - 1
//!   1 <= total <= bound <= 2**24
//! ```
//!
//! For one symbol with cumulative interval `[cum, cum+freq)`:
//!
//! ```text
//!   low   = low + (range / total) * cum
//!   range = (range / total) * freq
//! ```
//!
//! All products fit: `(2**32 - 1) * 2**24 < 2**56` (`u64`), and the
//! narrowed `range` fits in `u32` because
//! `freq * (range/total) <= total * floor(range/total) <= range`.
//!
//! # Normalization
//!
//! Whenever `range < 2**24`, the top byte of `low` (plus the carry bit) is
//! emitted and `range` and `low` are shifted left by 8.  Both encoder and
//! decoder normalize at the same points, so the same byte sequence is
//! consumed on both sides.
//!
//! # Carry propagation
//!
//! Emitted `0xFF` bytes are held back ("pending"): a later carry can turn
//! them into `0x00` while incrementing the last byte below `0xFF`.  The
//! state is therefore:
//!
//! * `cache` — the last byte strictly below `0xFF` (the anchor),
//! * `cache_size` — the number of bytes awaiting resolution (one anchor
//!   slot plus `0xFF` followers),
//! * `out` — bytes already resolved.
//!
//! The stream always starts with the seed byte `0x00` (the initial cache).
//! The decoder checks that byte; a wrong first byte means the stream is
//! not a range stream at all.
//!
//! # Termination / byte order
//!
//! After the last symbol the encoder runs five final shifts with `low`
//! frozen.  The first four push the remaining 32 bits of `low` out,
//! big-endian; the fifth is a forced flush with digit `0x00`, which
//! resolves every pending `0xFF` (no carry can follow afterwards).  A
//! legal stream therefore has **exactly** `5 + N` bytes where `N` is the
//! total number of normalization shifts, and the decoder consumes the
//! whole payload.  Any leftover or missing byte is an error.

use crate::error::{DecodeError, EncodeError};
use crate::table::FreqTable;

/// Normalization threshold: `range` is kept in the top 8 bits of a `u32`.
pub const TOP: u32 = 1 << 24;

/// Initial range, also the initial decoder range.
pub const INIT_RANGE: u32 = u32::MAX;

// ---------------------------------------------------------------------------
// Encoder
// ---------------------------------------------------------------------------

/// Streaming fixed-precision range encoder over a byte vector.
#[derive(Debug, Clone)]
pub struct RangeEncoder {
    out: Vec<u8>,
    cache: u8,
    cache_size: u64,
    low: u64,
    range: u32,
}

impl Default for RangeEncoder {
    fn default() -> Self {
        Self::new()
    }
}

impl RangeEncoder {
    /// Create an empty encoder with the canonical initial state.
    pub fn new() -> Self {
        Self {
            out: Vec::new(),
            cache: 0,
            cache_size: 1,
            low: 0,
            range: INIT_RANGE,
        }
    }

    /// Encode one symbol against a static table.
    ///
    /// Returns [`EncodeError::SymbolOutOfRange`] for an unknown symbol and
    /// [`EncodeError::ZeroFrequency`] when the symbol is declared but has
    /// zero probability — zero-frequency symbols cannot be represented.
    pub fn encode(&mut self, table: &FreqTable, symbol: u32) -> Result<(), EncodeError> {
        let alphabet = table.len() as u32;
        if symbol >= alphabet {
            return Err(EncodeError::SymbolOutOfRange { symbol, alphabet });
        }
        let (cum, next) = table
            .interval(symbol as usize)
            .expect("symbol just bounds-checked");
        let freq = next - cum;
        if freq == 0 {
            return Err(EncodeError::ZeroFrequency { symbol });
        }

        let total = table.total();
        let r = self.range / total;
        self.low += r as u64 * cum as u64;
        self.range = r * freq;

        while self.range < TOP {
            self.range <<= 8;
            self.shift_low();
        }
        Ok(())
    }

    /// Encode an entire symbol sequence.  Stops at the first illegal symbol
    /// and reports which one.
    pub fn encode_all(&mut self, table: &FreqTable, symbols: &[u32]) -> Result<(), EncodeError> {
        for &s in symbols {
            self.encode(table, s)?;
        }
        Ok(())
    }

    /// One normalization step: resolve the top byte of `low` (33 bits) into
    /// the pending-byte chain.
    fn shift_low(&mut self) {
        let low32 = self.low as u32;
        let carry = (self.low >> 32) as u8; // exactly 0 or 1
        if low32 < 0xFF00_0000 || carry != 0 {
            // Flush the anchor plus every pending follower.
            let anchor = self.cache;
            self.out.push(anchor.wrapping_add(carry));
            // Followers are 0xFF on no carry and 0x00 on carry.
            let follower = 0xFFu8.wrapping_add(carry);
            for _ in 1..self.cache_size {
                self.out.push(follower);
            }
            self.cache = (low32 >> 24) as u8;
            self.cache_size = 0;
        }
        self.cache_size += 1;
        // `(UInt32)Low << 8`: the resolved top byte already went through the
        // pending chain, so only the low 32 bits survive (wrapping shift).
        self.low = low32.wrapping_shl(8) as u64;
    }

    /// Finish the stream.  See the module documentation for why five shifts
    /// with the last digit forced to zero are sufficient.
    pub fn finish(mut self) -> Vec<u8> {
        for _ in 0..5 {
            self.shift_low();
        }
        debug_assert_eq!(
            self.cache_size, 1,
            "final forced flush must resolve all pending bytes"
        );
        self.out
    }

    /// Convenience: encode a whole message and return the finished bytes.
    pub fn encode_vec(table: &FreqTable, symbols: &[u32]) -> Result<Vec<u8>, EncodeError> {
        let mut enc = Self::new();
        enc.encode_all(table, symbols)?;
        Ok(enc.finish())
    }

    /// Current encoder range — exposed for step-by-step interval tests.
    pub fn range(&self) -> u32 {
        self.range
    }

    /// Current encoder low (33-bit value) — exposed for interval tests.
    pub fn low(&self) -> u64 {
        self.low
    }
}

// ---------------------------------------------------------------------------
// Decoder
// ---------------------------------------------------------------------------

/// Cursor over a borrowed byte slice that reports exact truncation offsets.
#[derive(Debug)]
struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    fn read(&mut self) -> Result<u8, DecodeError> {
        match self.buf.get(self.pos) {
            Some(&b) => {
                self.pos += 1;
                Ok(b)
            }
            None => Err(DecodeError::TruncatedStream {
                needed: 1,
                available: 0,
                at: self.pos as u64,
            }),
        }
    }
}

/// Fixed-precision range decoder.
///
/// Constructed over one finished payload; [`RangeDecoder::decode`] yields
/// exactly the declared number of symbols and then verifies that every
/// payload byte was consumed.
#[derive(Debug)]
pub struct RangeDecoder<'a> {
    cur: Cursor<'a>,
    code: u32,
    range: u32,
}

impl<'a> RangeDecoder<'a> {
    /// Initialize over a payload produced by [`RangeEncoder::finish`].
    ///
    /// Reads the seed byte plus four code bytes.  A corrupt seed is
    /// [`DecodeError::InvalidLeadingByte`]; a short buffer is
    /// [`DecodeError::TruncatedStream`] with the exact offset.
    pub fn new(payload: &'a [u8]) -> Result<Self, DecodeError> {
        let mut cur = Cursor::new(payload);
        let seed = cur.read()?;
        if seed != 0 {
            return Err(DecodeError::InvalidLeadingByte { got: seed });
        }
        let mut code = 0u32;
        for _ in 0..4 {
            code = (code << 8) | cur.read()? as u32;
        }
        Ok(Self {
            cur,
            code,
            range: INIT_RANGE,
        })
    }

    /// Decode one symbol.  `count` symbols in total will be read; the final
    /// full-consumption check belongs to [`RangeDecoder::finish_check`].
    pub fn decode(&mut self, table: &FreqTable) -> Result<u32, DecodeError> {
        let total = table.total();
        let r = self.range / total;
        let value = self.code / r;
        if value >= total {
            return Err(DecodeError::CodePointOutsideEnvelope { cum: value, total });
        }
        let symbol = table.symbol_for_cum(value);
        let (cum, next) = table
            .interval(symbol)
            .expect("partition_point returns an in-table symbol");
        if next <= value {
            // Landed on a zero-width (zero-frequency) entry.
            return Err(DecodeError::CodePointOutsideEnvelope { cum: value, total });
        }

        // Valid streams satisfy cum*r <= code; check anyway so corrupt
        // input causes an error instead of a wrapped subtraction.
        let sub = r * cum;
        self.code = self
            .code
            .checked_sub(sub)
            .ok_or(DecodeError::CodePointOutsideEnvelope { cum: value, total })?;
        self.range = r * (next - cum);

        while self.range < TOP {
            self.range <<= 8;
            let b = self.cur.read()?;
            self.code = (self.code << 8) | b as u32;
        }
        Ok(symbol as u32)
    }

    /// Verify the payload was consumed exactly.  Call after the declared
    /// number of symbols.
    pub fn finish_check(self) -> Result<(), DecodeError> {
        if self.cur.pos != self.cur.buf.len() {
            let remaining = self.cur.buf.len() - self.cur.pos;
            let got = self.cur.buf.get(self.cur.pos).copied();
            return Err(DecodeError::BadTerminator { remaining, got });
        }
        Ok(())
    }

    /// Bytes consumed so far (used in diagnostics and chunk accounting).
    pub fn bytes_consumed(&self) -> usize {
        self.cur.pos
    }

    /// Total payload length.
    pub fn payload_len(&self) -> usize {
        self.cur.buf.len()
    }
}

/// Decode exactly `count` symbols and verify the payload length.
pub fn decode_vec(
    table: &FreqTable,
    payload: &[u8],
    count: usize,
) -> Result<Vec<u32>, DecodeError> {
    let mut dec = RangeDecoder::new(payload)?;
    let mut out = Vec::with_capacity(count);
    for _ in 0..count {
        out.push(dec.decode(table)?);
    }
    dec.finish_check()?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t2() -> FreqTable {
        FreqTable::new(&[1, 1], 1 << 16).unwrap()
    }

    #[test]
    fn empty_message_is_five_zero_bytes() {
        let payload = RangeEncoder::encode_vec(&t2(), &[]).unwrap();
        assert_eq!(payload, vec![0, 0, 0, 0, 0]);
        let decoded = decode_vec(&t2(), &payload, 0).unwrap();
        assert!(decoded.is_empty());
    }

    #[test]
    fn single_symbol_known_bytes() {
        // [0] -> five zeros (low stays 0 throughout).
        assert_eq!(
            RangeEncoder::encode_vec(&t2(), &[0]).unwrap(),
            vec![0, 0, 0, 0, 0]
        );
        // [1] -> seed zero, 0x7F then three 0xFF followers.
        assert_eq!(
            RangeEncoder::encode_vec(&t2(), &[1]).unwrap(),
            vec![0x00, 0x7F, 0xFF, 0xFF, 0xFF]
        );
    }

    #[test]
    fn roundtrip_all_two_symbol_words_through_length_20() {
        let table = t2();
        for len in 0..=20 {
            for word in 0u32..(1u32 << len) {
                let symbols: Vec<u32> = (0..len).map(|i| (word >> i) & 1).collect();
                let payload = RangeEncoder::encode_vec(&table, &symbols).unwrap();
                let back = decode_vec(&table, &payload, len).unwrap();
                assert_eq!(back, symbols, "word {word} (len {len}) mismatched");
            }
        }
    }

    #[test]
    fn roundtrip_unequal_weights_bruteforce() {
        // Frequencies 1,2,3 over a small alphabet, exhaustive to depth 10.
        let table = FreqTable::new(&[1, 2, 3], 64).unwrap();
        let mut stack: Vec<u32> = vec![];
        fn walk(depth: usize, stack: &mut Vec<u32>, table: &FreqTable) {
            let payload = RangeEncoder::encode_vec(table, stack).unwrap();
            let back = decode_vec(table, &payload, stack.len()).unwrap();
            assert_eq!(&back, stack);
            if depth == 0 {
                return;
            }
            for s in 0..3 {
                stack.push(s);
                walk(depth - 1, stack, table);
                stack.pop();
            }
        }
        walk(10, &mut stack, &table);
    }

    #[test]
    fn zero_frequency_symbol_refused() {
        let table = FreqTable::new(&[1, 0, 2], 8).unwrap();
        let err = RangeEncoder::encode_vec(&table, &[1]).unwrap_err();
        assert_eq!(err, EncodeError::ZeroFrequency { symbol: 1 });
    }

    #[test]
    fn out_of_alphabet_symbol_refused() {
        let err = RangeEncoder::encode_vec(&t2(), &[7]).unwrap_err();
        assert_eq!(
            err,
            EncodeError::SymbolOutOfRange {
                symbol: 7,
                alphabet: 2
            }
        );
    }

    #[test]
    fn truncated_payloads_report_offsets() {
        let payload = RangeEncoder::encode_vec(&t2(), &[0, 1, 0, 1]).unwrap();
        for cut in 1..payload.len() {
            let err = decode_vec(&t2(), &payload[..cut], 4).unwrap_err();
            match err {
                DecodeError::TruncatedStream { at, .. } => {
                    assert_eq!(
                        at as usize, cut,
                        "error offset must point at first missing byte"
                    );
                }
                other => panic!("cut {cut}: expected truncation, got {other:?}"),
            }
        }
    }

    #[test]
    fn missing_init_seed_detected() {
        // Five bytes with a non-zero leading byte.
        let err = RangeDecoder::new(&[1, 0, 0, 0, 0]).unwrap_err();
        assert_eq!(err, DecodeError::InvalidLeadingByte { got: 1 });
    }

    #[test]
    fn corruption_is_detected_when_it_leaves_the_envelope() {
        // A flipped bit usually lands in another symbol's interval — the
        // raw arithmetic stream has no per-bit redundancy, so *integrity*
        // is the container layer's job (frame CRC; see container tests).
        // What the kernel itself guarantees: a point outside the frequency
        // envelope, or a truncated buffer, is a precise error, never UB or
        // a value outside the alphabet.
        let table = FreqTable::new(&[1, 1, 1, 1], 16).unwrap();
        let symbols = vec![0u32, 3, 2, 1, 0, 2, 3, 3, 1, 2, 0, 0, 3];
        let mut payload = RangeEncoder::encode_vec(&table, &symbols).unwrap();
        // Flip a high-impact byte; either rejected or decoded in-range, but
        // never out of the alphabet or with a length violation.
        payload[3] ^= 0x80;
        match decode_vec(&table, &payload, symbols.len()) {
            Ok(got) => {
                assert_eq!(got.len(), symbols.len());
                assert!(got.iter().all(|&s| s < 4));
            }
            Err(e) => assert!(matches!(
                e,
                DecodeError::CodePointOutsideEnvelope { .. }
                    | DecodeError::TruncatedStream { .. }
                    | DecodeError::InvalidLeadingByte { .. }
            )),
        }

        // A direct envelope violation: code point >= total must error.
        let bad = vec![0u8, 0xFF, 0xFF, 0xFF, 0xFF];
        let err = decode_vec(&table, &bad, 1).unwrap_err();
        assert!(matches!(err, DecodeError::CodePointOutsideEnvelope { .. }));
    }

    #[test]
    fn extra_bytes_after_stream_rejected() {
        let mut payload = RangeEncoder::encode_vec(&t2(), &[1]).unwrap();
        payload.push(0);
        let err = decode_vec(&t2(), &payload, 1).unwrap_err();
        match err {
            DecodeError::BadTerminator { remaining, .. } => assert_eq!(remaining, 1),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn declared_length_is_a_format_trust_boundary() {
        // Fundamental arithmetic-coding fact (same as LZMA): without an
        // explicit EOS symbol, nested intervals can share one code point.
        // [1], [1,0] and [1,0,0] all code to the same five bytes, so the
        // raw kernel trusts the *declared* symbol count — callers must bind
        // it to authenticated metadata.  The chunked container does exactly
        // that (frame symbol counts covered by CRC; see container tests).
        let p1 = RangeEncoder::encode_vec(&t2(), &[1]).unwrap();
        let p10 = RangeEncoder::encode_vec(&t2(), &[1, 0]).unwrap();
        let p100 = RangeEncoder::encode_vec(&t2(), &[1, 0, 0]).unwrap();
        assert_eq!(p1, p10);
        assert_eq!(p10, p100);

        // Over-declaring only fails once extra normalization bytes are
        // required; here it merely decodes further nested intervals.
        assert_eq!(decode_vec(&t2(), &p1, 3).unwrap(), vec![1, 0, 0]);

        // Under-declaring on a stream that emitted normalization bytes
        // leaves the final anchor byte unconsumed and IS caught; only the
        // degenerate zero-normalization case (a handful of leading symbols)
        // shares its code point with a longer sequence.
        let long = (0..50).map(|i| i % 2).collect::<Vec<_>>();
        let payload = RangeEncoder::encode_vec(&t2(), &long).unwrap();
        let err = decode_vec(&t2(), &payload, 3).unwrap_err();
        match err {
            DecodeError::BadTerminator { remaining, .. } => assert!(remaining >= 1),
            other => panic!("expected BadTerminator, got {other:?}"),
        }
        // The full declared length decodes correctly.
        assert_eq!(decode_vec(&t2(), &payload, 50).unwrap(), long);

        // What the raw layer CAN catch precisely: truncation.
        let cut = payload.len() - 2;
        let err = decode_vec(&t2(), &payload[..cut], 50).unwrap_err();
        assert!(matches!(err, DecodeError::TruncatedStream { at, .. } if at as usize == cut));
    }

    #[test]
    fn stepwise_intervals_match_formula() {
        // Encode symbol 1 against total=4, freqs [1,1,1,1], and check the
        // exact integer state after the (single) normalization.
        let table = FreqTable::uniform(4, 1 << 16).unwrap();
        let mut enc = RangeEncoder::new();
        enc.encode(&table, 1).unwrap();
        // r = 0xFFFFFFFF / 4 = 0x3FFFFFFF; low = r*1 = 0x3FFFFFFF.
        assert_eq!(enc.low(), 0x3FFF_FFFF);
        // range = r*1 = 0x3FFFFFFF, no normalization (>= 2^24).
        assert_eq!(enc.range(), 0x3FFF_FFFF);
    }
}
