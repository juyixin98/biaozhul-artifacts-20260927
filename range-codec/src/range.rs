//! Fixed-precision 32-bit range coder (integer arithmetic only).
//!
//! This is a Schindler-style byte-output range coder with an explicit carry
//! sentinel, so every normalisation and carry-propagation step is a plain
//! integer operation that can be compared bit-for-bit against the independent
//! Python reference in `tests/oracle/`.
//!
//! # Integer state
//!
//! | state   | meaning                                              |
//! |---------|------------------------------------------------------|
//! | `low`   | lower bound of the current interval, 33 bits max     |
//! | `range` | width of the current interval, always `>= TOP` just before a symbol is coded |
//!
//! Constants: `TOP = 2^24`, `BOT = 2^16`, frequency total `T <= 2^14`
//! (see [`crate::model::MAX_FREQ_TOTAL`]). Hence `range / T >= 2^10` before
//! every symbol step, keeping all scaled counts positive integers.
//!
//! # Normalisation & carry propagation
//!
//! Before coding each symbol, while `range < TOP`:
//!
//! 1. emit `(low >> 24) & 0xFF`; the 9th bit of that value (`0` or `1`) is a
//!    *carry* into the previously emitted bytes;
//! 2. if the carry is 1, walk backwards over `0xFF` bytes turning them into
//!    `0x00`, and increment the first non-`0xFF` byte; the walk terminates at
//!    the carry sentinel permanently stored at output byte 0;
//! 3. `low = (low << 8) mod 2^32`, `range <<= 8`.
//!
//! The output buffer always starts with a sentinel byte `0x00`. By the
//! integer interval invariant the sentinel only ever becomes `0x01`, so a
//! decoder accepts exactly `0x00` or `0x01` as its first byte; anything else
//! is corruption.
//!
//! # Tail convergence
//!
//! After the final symbol, four more normalisation-style shifts are emitted
//! (without touching `range`). Four bytes suffice: after the last symbol
//! `low < 2^33`; the first shift resolves the possible carry bit, and the
//! remaining three shifts publish the full 32-bit window the decoder reads.
//!
//! # Byte order
//!
//! Every multi-byte integer in the wire format is **big-endian**:
//! the decoder reads its initial 32-bit code from the four bytes after the
//! sentinel via `u32::from_be_bytes`, and consumes subsequent renormalisation
//! bytes high-byte first (`code = (code << 8) | byte`).

use crate::error::{CodecError, Result};
use crate::model::Model;

/// Renormalisation threshold (`2^24`).
pub const TOP: u32 = 1 << 24;
/// Lowest `range` can reach between two symbol steps before renorm (`2^16`).
pub const BOT: u32 = 1 << 16;
/// Number of tail shifts emitted at [`RangeEncoder::finish`].
pub const TAIL_SHIFTS: usize = 4;

/// One recorded coder step, used by step-by-step oracle comparison tests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncoderStep {
    pub index: usize,
    pub symbol: u32,
    pub cum: u32,
    pub freq: u32,
    pub low: u64,
    pub range: u32,
    /// Bytes emitted by renormalisation *before* this symbol.
    pub emitted: Vec<u8>,
}

/// One recorded decoder step.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecoderStep {
    pub index: usize,
    pub code: u32,
    pub range: u32,
    /// Scaled count `code / (range / total)`.
    pub scaled: u32,
    pub symbol: u32,
}

/// Streaming range encoder writing into a byte vector.
pub struct RangeEncoder {
    low: u64,
    range: u32,
    out: Vec<u8>,
    limit: Option<usize>,
    trace: Vec<EncoderStep>,
}

impl RangeEncoder {
    /// Create an encoder. The output starts with the carry sentinel `0x00`.
    pub fn new() -> Self {
        Self::with_limit(None)
    }

    /// Create an encoder with a hard output-size guard
    /// ([`CodecError::OutputLimitExceeded`]).
    pub fn with_limit(limit: Option<usize>) -> Self {
        RangeEncoder {
            low: 0,
            range: TOP,
            out: vec![0x00], // carry sentinel
            limit,
            trace: Vec::new(),
        }
    }

    fn guard(&self, extra: usize) -> Result<()> {
        if let Some(limit) = self.limit {
            if self.out.len() + extra > limit {
                return Err(CodecError::OutputLimitExceeded { limit });
            }
        }
        Ok(())
    }

    /// One shift-low step: publish the top byte of `low`, propagating a
    /// possible carry backwards over previously emitted `0xFF` bytes.
    fn shift_low(&mut self) -> Result<()> {
        self.guard(1)?;
        // low is at most 33 bits here; `val` is at most 0x1FF.
        let val = (self.low >> 24) as u32;
        let carry = val >> 8;
        let byte = (val & 0xFF) as u8;
        self.out.push(byte);

        if carry != 0 {
            // Walk back over the run of 0xFF bytes; the sentinel at index 0
            // always terminates the walk.
            let mut i = self.out.len() - 2;
            while i >= 1 && self.out[i] == 0xFF {
                self.out[i] = 0x00;
                i -= 1;
            }
            let v = self.out[i] as u16 + carry as u16;
            if v > 0xFF {
                // Defensive: mathematically unreachable; the sentinel bound
                // keeps it <= 1.
                return Err(CodecError::CarryOverflow);
            }
            self.out[i] = v as u8;
        }

        self.low = (self.low << 8) & 0xFFFF_FFFF;
        Ok(())
    }

    /// Renormalise until `range >= TOP` (called before each symbol).
    fn normalize(&mut self) -> Result<Vec<u8>> {
        let mut emitted = Vec::new();
        while self.range < TOP {
            let before = self.out.len();
            self.shift_low()?;
            emitted.push(self.out[before]);
            self.range = self.range.wrapping_shl(8);
        }
        Ok(emitted)
    }

    /// Encode one symbol against `model`, then let the model observe it.
    pub fn encode_symbol<M: Model>(&mut self, model: &mut M, symbol: u32) -> Result<()> {
        let idx = self.trace.len();
        let table = model.table();
        let (cum, freq) = table.interval_for(symbol)?;
        let total = table.total();

        let emitted = self.normalize()?;

        let step = (self.range as u64) / total as u64;
        self.low += step * cum as u64;
        self.range = (step * freq as u64) as u32;

        self.trace.push(EncoderStep {
            index: idx,
            symbol,
            cum,
            freq,
            low: self.low,
            range: self.range,
            emitted,
        });

        model.update(symbol)?;
        Ok(())
    }

    /// Encode every symbol of `input`.
    pub fn encode_all<M: Model>(&mut self, model: &mut M, input: &[u8]) -> Result<()> {
        for &b in input {
            self.encode_symbol(model, b as u32)?;
        }
        Ok(())
    }

    /// Emit the four tail shifts and return the finished byte stream.
    pub fn finish(mut self) -> Result<Vec<u8>> {
        for _ in 0..TAIL_SHIFTS {
            self.shift_low()?;
        }
        Ok(self.out)
    }

    /// Recorded encoder steps (for comparison tests).
    pub fn trace(&self) -> &[EncoderStep] {
        &self.trace
    }
}

impl Default for RangeEncoder {
    fn default() -> Self {
        Self::new()
    }
}

/// Streaming range decoder over a borrowed byte slice.
#[derive(Debug)]
pub struct RangeDecoder<'a> {
    code: u32,
    range: u32,
    data: &'a [u8],
    pos: usize,
    trace: Vec<DecoderStep>,
}

impl<'a> RangeDecoder<'a> {
    /// Consume the 5-byte preamble: carry sentinel plus the 32-bit code.
    pub fn new(data: &'a [u8]) -> Result<Self> {
        if data.len() < 5 {
            return Err(CodecError::Truncated {
                needed: 5,
                available: data.len(),
            });
        }
        let init = data[0];
        // The sentinel is 0 (no carry across the whole stream) or 1 (the
        // accumulated top carry). Anything else is corrupt framing.
        if init > 1 {
            return Err(CodecError::InvalidInitByte { found: init });
        }
        let mut code_bytes = [0u8; 4];
        code_bytes.copy_from_slice(&data[1..5]);
        let code = u32::from_be_bytes(code_bytes);
        Ok(RangeDecoder {
            code,
            range: TOP,
            data,
            pos: 5,
            trace: Vec::new(),
        })
    }

    fn read_byte(&mut self) -> Result<u8> {
        if self.pos >= self.data.len() {
            return Err(CodecError::Truncated {
                needed: self.pos + 1,
                available: self.data.len(),
            });
        }
        let b = self.data[self.pos];
        self.pos += 1;
        Ok(b)
    }

    fn normalize(&mut self) -> Result<()> {
        while self.range < TOP {
            let b = self.read_byte()?;
            self.code = (self.code << 8) | b as u32;
            self.range = self.range.wrapping_shl(8);
        }
        Ok(())
    }

    /// Decode the next symbol, then let the model observe the result so
    /// encoder/decoder tables stay in lockstep.
    pub fn decode_symbol<M: Model + ?Sized>(&mut self, model: &mut M) -> Result<u32> {
        let idx = self.trace.len();
        self.normalize()?;
        let table = model.table();
        let total = table.total();

        let step = (self.range as u64) / total as u64;
        let scaled = (self.code as u64 / step) as u32;
        let symbol = table.symbol_for(scaled)?;
        let (cum, freq) = table.interval_for(symbol)?;

        self.code = (self.code as u64 - step * cum as u64) as u32;
        self.range = (step * freq as u64) as u32;

        self.trace.push(DecoderStep {
            index: idx,
            code: self.code,
            range: self.range,
            scaled,
            symbol,
        });

        model.update(symbol)?;
        Ok(symbol)
    }

    /// Number of input bytes consumed so far.
    pub fn bytes_consumed(&self) -> usize {
        self.pos
    }

    /// Recorded decoder steps.
    pub fn trace(&self) -> &[DecoderStep] {
        &self.trace
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::StaticModel;

    fn roundtrip(freqs: &[u32], input: &[u8]) -> Vec<u8> {
        let mut enc = RangeEncoder::new();
        let mut m = StaticModel::new(freqs).unwrap();
        enc.encode_all(&mut m, input).unwrap();
        let bytes = enc.finish().unwrap();

        let mut dec = RangeDecoder::new(&bytes).unwrap();
        let mut m2 = StaticModel::new(freqs).unwrap();
        let mut out = Vec::new();
        for _ in 0..input.len() {
            out.push(dec.decode_symbol(&mut m2).unwrap() as u8);
        }
        assert_eq!(out, input, "roundtrip mismatch");
        bytes
    }

    #[test]
    fn empty_input_emits_sentinel_plus_four_tail_bytes() {
        let mut enc = RangeEncoder::new();
        let mut m = StaticModel::new(&[1]).unwrap();
        enc.encode_all(&mut m, &[]).unwrap();
        let bytes = enc.finish().unwrap();
        assert_eq!(bytes.len(), 5, "sentinel + 4 tail shifts");
        assert_eq!(bytes[0], 0);
        // Decoder constructs without symbols.
        let dec = RangeDecoder::new(&bytes).unwrap();
        assert_eq!(dec.bytes_consumed(), 5);
    }

    #[test]
    fn single_alphabet_long_repeat() {
        // Only symbol 0 exists; any length must decode back exactly.
        let n = 5000usize;
        let input = vec![0u8; n];
        let bytes = roundtrip(&[1], &input);
        // Compression: effectively one byte per 8 symbols + framing.
        assert!(bytes.len() < 700, "got {}", bytes.len());
    }

    #[test]
    fn alternating_two_symbols() {
        // 3-symbol alphabet where symbol 1 has zero frequency; the stream
        // only ever uses symbols 0 and 2.
        let input: Vec<u8> = (0..4096).map(|i| if i % 2 == 0 { 0u8 } else { 2 }).collect();
        roundtrip(&[3, 0, 5], &input);
    }

    #[test]
    fn all_symbols_alphabet() {
        let freqs = [1u32; 256];
        let input: Vec<u8> = (0..=255u16).map(|x| x as u8).collect();
        roundtrip(&freqs, &input);
    }

    #[test]
    fn input_of_length_one_each_symbol() {
        let freqs = [1u32, 1u32, 1u32];
        for s in 0..3u8 {
            let bytes = roundtrip(&freqs, &[s]);
            assert!(bytes.len() >= 5);
        }
    }

    #[test]
    fn truncated_preamble_is_an_exact_error() {
        let err = RangeDecoder::new(&[0, 0, 0]).unwrap_err();
        assert!(matches!(
            err,
            CodecError::Truncated {
                needed: 5,
                available: 3
            }
        ));
    }

    #[test]
    fn bad_init_byte_is_rejected() {
        let mut enc = RangeEncoder::new();
        let mut m = StaticModel::new(&[1, 1]).unwrap();
        enc.encode_all(&mut m, &[0]).unwrap();
        let mut bytes = enc.finish().unwrap();
        bytes[0] = 0x7F;
        let err = RangeDecoder::new(&bytes).unwrap_err();
        assert_eq!(err, CodecError::InvalidInitByte { found: 0x7F });
        assert_eq!(err.decision(), crate::error::Decision::Rejected);
    }

    #[test]
    fn truncation_mid_stream_is_rejected_not_panicked() {
        let freqs = [1u32, 1, 1, 1];
        let input: Vec<u8> = (0..200u16).map(|i| (i % 4) as u8).collect();
        let mut enc = RangeEncoder::new();
        let mut m = StaticModel::new(&freqs).unwrap();
        enc.encode_all(&mut m, &input).unwrap();
        let bytes = enc.finish().unwrap();
        // Cut most of the tail off; the decoder must report TRUNCATED.
        let cut = bytes.len() - 3;
        let mut dec = RangeDecoder::new(&bytes[..cut]).unwrap();
        let mut m2 = StaticModel::new(&freqs).unwrap();
        let mut got_truncated = false;
        for _ in 0..input.len() {
            match dec.decode_symbol(&mut m2) {
                Ok(_) => {}
                Err(CodecError::Truncated { .. }) => {
                    got_truncated = true;
                    break;
                }
                Err(e) => panic!("unexpected error {e:?}"),
            }
        }
        assert!(got_truncated, "decoder should hit a truncated read");
    }

    #[test]
    fn carry_propagates_backwards_over_ff_run() {
        // Independently derived (Python oracle): equal frequencies and the
        // sequence seven 0s then a 1 drives a carry through a run of two
        // preceding 0xFF bytes, turning them into 0x00 and incrementing the
        // byte before. Exact bytes are asserted, not just roundtrip.
        let mut enc = RangeEncoder::new();
        let mut m = StaticModel::new(&[1, 1]).unwrap();
        enc.encode_all(&mut m, &[0, 0, 0, 0, 0, 0, 0, 1]).unwrap();
        let bytes = enc.finish().unwrap();
        assert_eq!(
            bytes,
            vec![0x00, 0x00, 0x01, 0x00, 0x00, 0x00],
            "carry must back-fill the 0xFF run to 0x00"
        );

        // Roundtrip still exact.
        let mut dec = RangeDecoder::new(&bytes).unwrap();
        let mut m2 = StaticModel::new(&[1, 1]).unwrap();
        let out: Vec<u8> = (0..8).map(|_| dec.decode_symbol(&mut m2).unwrap() as u8).collect();
        assert_eq!(out, vec![0, 0, 0, 0, 0, 0, 0, 1]);
    }

    #[test]
    fn sentinel_is_always_zero_for_a_fresh_stream() {
        // Across many first-symbol choices the first emitted byte (sentinel)
        // must remain 0x00; carry only ever reaches it on later steps.
        for freqs in [vec![1u32, 1], vec![1, 1, 1, 1], vec![7, 3]] {
            let mut enc = RangeEncoder::new();
            let mut m = StaticModel::new(&freqs).unwrap();
            enc.encode_symbol(&mut m, 0).unwrap();
            let bytes = enc.finish().unwrap();
            assert_eq!(bytes[0], 0x00);
        }
    }

    #[test]
    fn encoder_and_decoder_intervals_agree_step_by_step() {
        let freqs = [2u32, 1, 3];
        let input = [0u8, 2, 1, 2, 0, 2];
        let mut enc = RangeEncoder::new();
        let mut m1 = StaticModel::new(&freqs).unwrap();
        enc.encode_all(&mut m1, &input).unwrap();
        let etrace: Vec<_> = enc.trace().to_vec();
        let bytes = enc.finish().unwrap();

        let mut dec = RangeDecoder::new(&bytes).unwrap();
        let mut m2 = StaticModel::new(&freqs).unwrap();
        for (i, &want) in input.iter().enumerate() {
            let got = dec.decode_symbol(&mut m2).unwrap();
            assert_eq!(got, want as u32, "symbol {i}");
        }
        let dtrace = dec.trace();
        assert_eq!(dtrace.len(), etrace.len());
        // Decoder's final range must equal the encoder's final range at
        // every corresponding step.
        for (e, d) in etrace.iter().zip(dtrace.iter()) {
            assert_eq!(e.range, d.range, "range at step {}", e.index);
            assert_eq!(e.symbol, d.symbol);
        }
    }

    #[test]
    fn output_limit_is_enforced() {
        let mut enc = RangeEncoder::with_limit(Some(6));
        let mut m = StaticModel::new(&[1, 1]).unwrap();
        // One symbol can emit several bytes; insist the guard fires.
        let res = std::iter::repeat_n(1u8, 100)
            .try_for_each(|s| enc.encode_symbol(&mut m, s as u32));
        assert!(matches!(
            res,
            Err(CodecError::OutputLimitExceeded { limit: 6 })
        ));
    }
}
