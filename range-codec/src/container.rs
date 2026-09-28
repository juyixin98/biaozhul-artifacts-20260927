//! Chunked container format (`RC01`).
//!
//! The container wraps a sequence of independently range-coded chunks and
//! adds framing, length declarations, per-chunk checksums and resource
//! budgets. Everything a decoder needs to trust a stream is declared before
//! the coded payload, so budgets are enforced *before* decode work starts.
//!
//! # On-disk layout (all multi-byte integers big-endian)
//!
//! ```text
//! header := magic[4] version u8 flags u8
//!           num_symbols u16 declared_len u64 num_chunks u32
//! chunk* := tag u8 sym_count u32 payload_len u32
//!           [freq u32 * num_symbols]      # static mode only
//!           payload[payload_len]
//!           crc32 u32                      # over tag..payload (this chunk)
//! end    := tag u8 = 0x45 ('E')
//! ```
//!
//! * magic = `"RC01"` (`52 43 30 31`); `version = 1`.
//! * flags: bit 0 selects the model — `0` static (every chunk carries its
//!   own frequency table), `1` adaptive (each chunk starts a fresh
//!   uniform-frequency adaptive model; no tables are stored). Bits 1..=7 are
//!   reserved and MUST be zero; setting them yields
//!   [`CodecError::ReservedFlag`] with an *indeterminate* decision.
//! * `declared_len` is the total number of decoded symbols across chunks;
//!   the decoder rejects mismatch before returning.
//! * `num_symbols` is in `1..=256`.
//! * Each data chunk has `1 <= sym_count <= MAX_CHUNK_SYMBOLS (65535)`.
//!   `num_chunks` may be zero only for empty input; the END chunk is always
//!   present.
//! * CRC-32 is ISO-HDLC (`zlib.crc32` compatible, see [`crate::crc`]).
//!
//! The range-coded payload of one chunk is exactly the byte stream described
//! in [`crate::range`]: carry sentinel, code window, tail.

use crate::crc::Crc32;
use crate::error::{CodecError, Result};
use crate::model::{AdaptiveModel, FrequencyTable, Model, StaticModel, MAX_SYMBOLS};
use crate::range::RangeDecoder;
use crate::range::RangeEncoder;

/// Container magic bytes `RC01`.
pub const MAGIC: [u8; 4] = [0x52, 0x43, 0x30, 0x31];
/// Supported container version.
pub const VERSION: u8 = 1;
/// Data chunk tag.
pub const TAG_DATA: u8 = 0xD0;
/// End chunk tag.
pub const TAG_END: u8 = 0x45;

const FLAG_ADAPTIVE: u8 = 0x01;
const RESERVED_MASK: u8 = !FLAG_ADAPTIVE;

/// Maximum symbols declared per chunk.
pub const MAX_CHUNK_SYMBOLS: u32 = 65_535;

/// Model selection for a container.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModelMode {
    /// Every chunk carries its own static frequency table.
    Static,
    /// Every chunk starts a fresh uniform (`f=1`) adaptive model.
    Adaptive,
}

impl ModelMode {
    fn flag(self) -> u8 {
        match self {
            ModelMode::Static => 0,
            ModelMode::Adaptive => FLAG_ADAPTIVE,
        }
    }
}

/// Decoder-side resource budgets. The decoder never exceeds these.
#[derive(Debug, Clone)]
pub struct DecodeBudget {
    /// Maximum declared total symbol count.
    pub max_symbol_count: u64,
    /// Maximum number of data chunks.
    pub max_chunks: usize,
    /// Maximum total range-coded payload bytes.
    pub max_payload_bytes: usize,
    /// Maximum symbols a single chunk may declare.
    pub max_chunk_symbols: u32,
}

impl Default for DecodeBudget {
    fn default() -> Self {
        DecodeBudget {
            max_symbol_count: 1 << 24,      // 16 Mi symbols
            max_chunks: 4096,
            max_payload_bytes: 64 << 20,   // 64 MiB
            max_chunk_symbols: MAX_CHUNK_SYMBOLS,
        }
    }
}

/// One chunk with a static table (used by the static encode API).
#[derive(Debug, Clone)]
pub struct StaticChunk {
    pub freqs: Vec<u32>,
    pub symbols: Vec<u8>,
}

/// Decode result.
#[derive(Debug, Clone)]
pub struct Decoded {
    pub symbols: Vec<u8>,
    pub chunks: usize,
    pub mode: ModelMode,
    pub num_symbols: u16,
}

// ---------------------------------------------------------------- reading

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }
    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        if self.pos + n > self.buf.len() {
            return Err(CodecError::Truncated {
                needed: n,
                available: self.buf.len() - self.pos,
            });
        }
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16> {
        let s = self.take(2)?;
        Ok(u16::from_be_bytes([s[0], s[1]]))
    }
    fn u32(&mut self) -> Result<u32> {
        let s = self.take(4)?;
        Ok(u32::from_be_bytes([s[0], s[1], s[2], s[3]]))
    }
    fn u64(&mut self) -> Result<u64> {
        let s = self.take(8)?;
        Ok(u64::from_be_bytes([
            s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7],
        ]))
    }
}

fn be_u16(v: u16) -> [u8; 2] {
    v.to_be_bytes()
}
fn be_u32(v: u32) -> [u8; 4] {
    v.to_be_bytes()
}
fn be_u64(v: u64) -> [u8; 8] {
    v.to_be_bytes()
}

fn validate_num_symbols(n: u16) -> Result<()> {
    if n == 0 || u32::from(n) > MAX_SYMBOLS {
        return Err(CodecError::BadAlphabetSize {
            size: n as usize,
            max: MAX_SYMBOLS,
        });
    }
    Ok(())
}

/// Decode and fully validate a container under `budget`.
pub fn decode(data: &[u8], budget: &DecodeBudget) -> Result<Decoded> {
    let mut r = Reader::new(data);

    // ---- header --------------------------------------------------------
    let magic: [u8; 4] = r.take(4)?.try_into().unwrap();
    if magic != MAGIC {
        return Err(CodecError::BadMagic { found: magic });
    }
    let version = r.u8()?;
    if version != VERSION {
        return Err(CodecError::UnsupportedVersion { found: version });
    }
    let flags = r.u8()?;
    if flags & RESERVED_MASK != 0 {
        // Identify the highest reserved bit that is set.
        let reserved = flags & RESERVED_MASK;
        let bit = 7 - reserved.leading_zeros(); // u8 leading zeros, 0..=6
        return Err(CodecError::ReservedFlag { flag: 1u8 << bit });
    }
    let mode = if flags & FLAG_ADAPTIVE != 0 {
        ModelMode::Adaptive
    } else {
        ModelMode::Static
    };
    let num_symbols = r.u16()?;
    validate_num_symbols(num_symbols)?;
    let declared_len = r.u64()?;
    let num_chunks = r.u32()?;

    // ---- budget checks up front ---------------------------------------
    if declared_len > budget.max_symbol_count {
        return Err(CodecError::LengthBudgetExceeded {
            declared: declared_len,
            budget: budget.max_symbol_count,
        });
    }
    if num_chunks as usize > budget.max_chunks {
        return Err(CodecError::ChunkBudgetExceeded {
            chunks: num_chunks as usize,
            budget: budget.max_chunks,
        });
    }
    // Sum of chunk symbol counts cannot overflow u64 given the per-chunk cap.
    let max_possible = num_chunks as u64 * budget.max_chunk_symbols as u64;
    if max_possible > budget.max_symbol_count {
        return Err(CodecError::LengthBudgetExceeded {
            declared: max_possible,
            budget: budget.max_symbol_count,
        });
    }

    let mut symbols: Vec<u8> = Vec::with_capacity(declared_len.min(1 << 20) as usize);
    let mut total_payload: usize = 0;
    let mut running_len: u64 = 0;

    for idx in 0..num_chunks as usize {
        // CRC window starts at the tag byte.
        let crc_start = r.pos;
        let tag = r.u8()?;
        if tag != TAG_DATA {
            return Err(CodecError::UnknownChunkType { tag });
        }
        let sym_count = r.u32()?;
        if sym_count == 0 || sym_count > budget.max_chunk_symbols {
            return Err(CodecError::BadChunkLength {
                index: idx,
                length: sym_count,
            });
        }
        running_len += sym_count as u64;
        if running_len > declared_len {
            return Err(CodecError::LengthMismatch {
                declared: declared_len,
                decoded: running_len,
            });
        }

        let payload_len = r.u32()? as usize;
        total_payload += payload_len;
        if total_payload > budget.max_payload_bytes {
            return Err(CodecError::ByteBudgetExceeded {
                used: total_payload,
                budget: budget.max_payload_bytes,
            });
        }

        // Static tables are read and validated before any range decoding.
        let freqs = if let ModelMode::Static = mode {
            let mut freqs = Vec::with_capacity(num_symbols as usize);
            for _ in 0..num_symbols {
                freqs.push(r.u32()?);
            }
            FrequencyTable::new(&freqs)?;
            freqs
        } else {
            Vec::new()
        };

        let payload = r.take(payload_len)?;
        let stored_crc = r.u32()?;

        // Verify checksum before any kernel work on the payload.
        let computed = Crc32::checksum(&data[crc_start..r.pos - 4]);
        if computed != stored_crc {
            return Err(CodecError::CrcMismatch {
                index: idx,
                stored: stored_crc,
                computed,
            });
        }

        // ---- range decode this chunk -----------------------------------
        let mut dec = RangeDecoder::new(payload)?;
        let mut model: Box<dyn Model> = match mode {
            ModelMode::Static => Box::new(StaticModel::new(&freqs)?),
            ModelMode::Adaptive => {
                Box::new(AdaptiveModel::uniform(u32::from(num_symbols), 1)?)
            }
        };
        for _ in 0..sym_count {
            let s = dec.decode_symbol(model.as_mut())?;
            if s >= u32::from(num_symbols) {
                return Err(CodecError::SymbolOutOfRange {
                    symbol: s,
                    num_symbols: u32::from(num_symbols),
                });
            }
            symbols.push(s as u8);
        }
    }

    if running_len != declared_len {
        return Err(CodecError::LengthMismatch {
            declared: declared_len,
            decoded: running_len,
        });
    }

    let end_tag = r.u8()?;
    if end_tag != TAG_END {
        return Err(CodecError::UnknownChunkType { tag: end_tag });
    }
    if r.pos != data.len() {
        return Err(CodecError::TrailingBytes {
            count: data.len() - r.pos,
        });
    }

    Ok(Decoded {
        symbols,
        chunks: num_chunks as usize,
        mode,
        num_symbols,
    })
}

// ---------------------------------------------------------------- writing

fn write_header(out: &mut Vec<u8>, mode: ModelMode, num_symbols: u16, declared: u64, chunks: u32) {
    out.extend_from_slice(&MAGIC);
    out.push(VERSION);
    out.push(mode.flag());
    out.extend_from_slice(&be_u16(num_symbols));
    out.extend_from_slice(&be_u64(declared));
    out.extend_from_slice(&be_u32(chunks));
}

fn append_crc(out: &mut Vec<u8>, crc_start: usize) {
    let crc = Crc32::checksum(&out[crc_start..]);
    out.extend_from_slice(&be_u32(crc));
}

/// Encode chunks, each carrying its own static frequency table.
///
/// Every chunk's table must have the same alphabet size and be valid per
/// [`FrequencyTable::new`]; symbols outside a chunk's alphabet or with zero
/// frequency are rejected with the exact kernel error.
pub fn encode_static(chunks: &[StaticChunk]) -> Result<Vec<u8>> {
    let num_symbols = chunks
        .first()
        .map(|c| c.freqs.len())
        .unwrap_or(1) as u16;
    validate_num_symbols(num_symbols)?;
    for (i, c) in chunks.iter().enumerate() {
        if c.freqs.len() != num_symbols as usize {
            return Err(CodecError::BadAlphabetSize {
                size: c.freqs.len(),
                max: MAX_SYMBOLS,
            });
        }
        if c.symbols.is_empty() || c.symbols.len() > MAX_CHUNK_SYMBOLS as usize {
            return Err(CodecError::BadChunkLength {
                index: i,
                length: c.symbols.len() as u32,
            });
        }
    }

    let declared: u64 = chunks.iter().map(|c| c.symbols.len() as u64).sum();
    let mut out = Vec::new();
    write_header(
        &mut out,
        ModelMode::Static,
        num_symbols,
        declared,
        chunks.len() as u32,
    );

    for chunk in chunks.iter() {
        let table = FrequencyTable::new(&chunk.freqs)?;
        let mut enc = RangeEncoder::new();
        let mut model = StaticModel::from_table(table);
        enc.encode_all(&mut model, &chunk.symbols)?;
        let payload = enc.finish()?;

        let crc_start = out.len();
        out.push(TAG_DATA);
        out.extend_from_slice(&be_u32(chunk.symbols.len() as u32));
        out.extend_from_slice(&be_u32(payload.len() as u32));
        for &f in &chunk.freqs {
            out.extend_from_slice(&be_u32(f));
        }
        out.extend_from_slice(&payload);
        append_crc(&mut out, crc_start);
    }

    out.push(TAG_END);
    Ok(out)
}

/// Encode chunks with a fresh uniform adaptive model per chunk.
pub fn encode_adaptive(num_symbols: u16, chunks: &[Vec<u8>]) -> Result<Vec<u8>> {
    validate_num_symbols(num_symbols)?;
    for (i, syms) in chunks.iter().enumerate() {
        if syms.is_empty() || syms.len() > MAX_CHUNK_SYMBOLS as usize {
            return Err(CodecError::BadChunkLength {
                index: i,
                length: syms.len() as u32,
            });
        }
        for &s in syms {
            if u32::from(s) >= u32::from(num_symbols) {
                return Err(CodecError::SymbolOutOfRange {
                    symbol: u32::from(s),
                    num_symbols: u32::from(num_symbols),
                });
            }
        }
    }

    let declared: u64 = chunks.iter().map(|c| c.len() as u64).sum();
    let mut out = Vec::new();
    write_header(
        &mut out,
        ModelMode::Adaptive,
        num_symbols,
        declared,
        chunks.len() as u32,
    );

    for syms in chunks {
        let mut enc = RangeEncoder::new();
        let mut model = AdaptiveModel::uniform(u32::from(num_symbols), 1)?;
        enc.encode_all(&mut model, syms)?;
        let payload = enc.finish()?;

        let crc_start = out.len();
        out.push(TAG_DATA);
        out.extend_from_slice(&be_u32(syms.len() as u32));
        out.extend_from_slice(&be_u32(payload.len() as u32));
        out.extend_from_slice(&payload);
        append_crc(&mut out, crc_start);
    }

    out.push(TAG_END);
    Ok(out)
}

/// Encode empty input (zero chunks) in either mode.
pub fn encode_empty(mode: ModelMode, num_symbols: u16) -> Result<Vec<u8>> {
    validate_num_symbols(num_symbols)?;
    let mut out = Vec::new();
    write_header(&mut out, mode, num_symbols, 0, 0);
    out.push(TAG_END);
    Ok(out)
}

/// Convenience: split one byte slice into fixed-size chunks.
pub fn split_chunks(input: &[u8], chunk_size: usize) -> Vec<Vec<u8>> {
    if input.is_empty() {
        return Vec::new();
    }
    input
        .chunks(chunk_size.max(1).min(MAX_CHUNK_SYMBOLS as usize))
        .map(|c| c.to_vec())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget() -> DecodeBudget {
        DecodeBudget::default()
    }

    #[test]
    fn empty_container_roundtrip_both_modes() {
        for mode in [ModelMode::Static, ModelMode::Adaptive] {
            let data = encode_empty(mode, 256).unwrap();
            let d = decode(&data, &budget()).unwrap();
            assert!(d.symbols.is_empty());
            assert_eq!(d.chunks, 0);
            assert_eq!(d.mode, mode);
        }
    }

    #[test]
    fn static_multi_chunk_roundtrip() {
        let input: Vec<u8> = (0..5000u32).map(|i| (i % 3) as u8).collect();
        let parts = split_chunks(&input, 1000);
        let chunks: Vec<_> = parts
            .iter()
            .map(|s| StaticChunk {
                freqs: vec![10, 20, 30],
                symbols: s.clone(),
            })
            .collect();
        let data = encode_static(&chunks).unwrap();
        let d = decode(&data, &budget()).unwrap();
        assert_eq!(d.symbols, input);
        assert_eq!(d.chunks, 5);
    }

    #[test]
    fn adaptive_mode_roundtrip() {
        let input: Vec<u8> = (0..20_000u32)
            .map(|i| ((i * 7 + (i >> 3)) % 256) as u8)
            .collect();
        let parts = split_chunks(&input, 4096);
        let data = encode_adaptive(256, &parts).unwrap();
        let d = decode(&data, &budget()).unwrap();
        assert_eq!(d.symbols, input);
        assert_eq!(d.chunks, 5);
    }

    #[test]
    fn bad_magic_is_exact_error() {
        let mut data = encode_empty(ModelMode::Static, 2).unwrap();
        data[0] = b'X';
        assert!(matches!(
            decode(&data, &budget()),
            Err(CodecError::BadMagic { .. })
        ));
    }

    #[test]
    fn unsupported_version_is_indeterminate_rejected() {
        let mut data = encode_empty(ModelMode::Static, 2).unwrap();
        data[4] = 99;
        let err = decode(&data, &budget()).unwrap_err();
        assert!(matches!(err, CodecError::UnsupportedVersion { found: 99 }));
        assert_eq!(err.code(), "UNSUPPORTED_VERSION");
    }

    #[test]
    fn reserved_flag_is_indeterminate() {
        let mut data = encode_empty(ModelMode::Static, 2).unwrap();
        data[5] = 0x80;
        let err = decode(&data, &budget()).unwrap_err();
        match err {
            CodecError::ReservedFlag { flag } => assert_eq!(flag, 0x80),
            other => panic!("wrong error {other:?}"),
        }
        assert_eq!(err.decision(), crate::error::Decision::Indeterminate);
    }

    #[test]
    fn truncated_header_reports_needed_and_available() {
        let err = decode(&MAGIC[..], &budget()).unwrap_err();
        match err {
            CodecError::Truncated { needed, available } => {
                assert_eq!(needed, 1);
                assert_eq!(available, 0);
            }
            other => panic!("wrong error {other:?}"),
        }
    }

    #[test]
    fn length_budget_rejected_before_decode() {
        let data = encode_empty(ModelMode::Static, 2).unwrap();
        let b = DecodeBudget {
            max_symbol_count: 0,
            ..DecodeBudget::default()
        };
        // Empty input has declared_len 0, fine; now check a real one.
        assert!(decode(&data, &b).is_ok());

        let chunk = StaticChunk {
            freqs: vec![1, 1],
            symbols: vec![0, 1, 0, 1],
        };
        let data = encode_static(&[chunk]).unwrap();
        let err = decode(&data, &b).unwrap_err();
        assert!(matches!(
            err,
            CodecError::LengthBudgetExceeded { declared: 4, budget: 0 }
        ));
    }

    #[test]
    fn crc_tampering_is_detected() {
        let chunk = StaticChunk {
            freqs: vec![1, 1, 1],
            symbols: vec![0, 1, 2, 0, 1],
        };
        let mut data = encode_static(&[chunk]).unwrap();
        // Flip a payload byte. Find payload by locating last 5 bytes region;
        // payload sits before final CRC and END.
        let n = data.len();
        data[n - 6] ^= 0xFF;
        let err = decode(&data, &budget()).unwrap_err();
        assert!(
            matches!(err, CodecError::CrcMismatch { index: 0, .. }),
            "got {err:?}"
        );
    }

    #[test]
    fn trailing_bytes_after_end_rejected() {
        let mut data = encode_empty(ModelMode::Static, 2).unwrap();
        data.push(0x00);
        assert!(matches!(
            decode(&data, &budget()),
            Err(CodecError::TrailingBytes { count: 1 })
        ));
    }

    #[test]
    fn illegal_static_table_in_chunk_is_exact_error() {
        let chunk = StaticChunk {
            freqs: vec![0, 0], // total zero
            symbols: vec![0],
        };
        assert!(matches!(
            encode_static(&[chunk]),
            Err(CodecError::FrequencyTotalOutOfBounds { total: 0, .. })
        ));
    }

    #[test]
    fn zero_frequency_symbol_rejected_in_container_encode() {
        let chunk = StaticChunk {
            freqs: vec![1, 0, 2],
            symbols: vec![1],
        };
        let err = encode_static(&[chunk]).unwrap_err();
        assert_eq!(err, CodecError::ZeroFrequencySymbol { symbol: 1 });
    }

    #[test]
    fn byte_budget_is_enforced() {
        let chunk = StaticChunk {
            freqs: vec![1; 256],
            symbols: (0..256u16).map(|i| i as u8).collect(),
        };
        let data = encode_static(&[chunk]).unwrap();
        let b = DecodeBudget {
            max_payload_bytes: 4,
            ..DecodeBudget::default()
        };
        assert!(matches!(
            decode(&data, &b),
            Err(CodecError::ByteBudgetExceeded { .. })
        ));
    }
}
