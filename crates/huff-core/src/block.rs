//! One independently decodable block payload (FORMAT.md §6).
//!
//! Payload fields (little-endian, fixed 12-byte header):
//!
//! | offset | size | field          | notes                                   |
//! |--------|------|----------------|-----------------------------------------|
//! | 0      | 4    | `original_len` | bytes of decoded data in this block     |
//! | 4      | 4    | `bits_total`   | valid bits in the bitstream body        |
//! | 8      | 2    | `symbol_count` | 0..255                                  |
//! | 10     | 1    | `sole_symbol`  | the symbol when count == 1, else 0      |
//! | 11     | 1    | `reserved`     | must be zero                            |
//! | 12     | 256  | code lengths   | one byte per alphabet symbol            |
//! | 268    | N    | bitstream body | MSB-first, zero-padded to a byte        |

use crate::bits::{check_zero_padding, BitReader, BitWriter};
use crate::error::{HuffError, Result};
use crate::huff::{CanonTable, Codebook};

/// Fixed payload header size (`u32+u32+u16+u8+u8`).
pub const PAYLOAD_HEADER_LEN: usize = 12;
/// Code-length table size (256 symbols).
pub const LEN_TABLE_LEN: usize = 256;
/// Payload offset where the bitstream body starts.
pub const BODY_OFFSET: usize = PAYLOAD_HEADER_LEN + LEN_TABLE_LEN;

/// Encode one block (at most one block-size worth of input).
pub fn encode_block(data: &[u8]) -> Result<Vec<u8>> {
    let original_len =
        u32::try_from(data.len()).map_err(|_| HuffError::PayloadLengthMismatch)?;

    let mut lengths = [0u8; 256];
    let mut sole_symbol: u8 = 0;
    let mut symbol_count: u16 = 0;

    let mut body = Vec::new();
    let mut bits_total: u32 = 0;

    if data.is_empty() {
        // Empty block: empty alphabet, no body bits.
    } else {
        let mut freq = [0u64; 256];
        for &b in data {
            freq[b as usize] += 1;
        }
        let codebook = Codebook::from_frequencies(&freq)?;
        lengths = codebook.lengths;
        symbol_count = codebook.present.len() as u16;
        if symbol_count == 1 {
            sole_symbol = codebook.present[0];
        } else {
            let mut w = BitWriter::new();
            for &b in data {
                codebook.encode_symbol(&mut w, b);
            }
            let (encoded, bits) = w.finish();
            body = encoded;
            bits_total = u32::try_from(bits).map_err(|_| HuffError::PayloadLengthMismatch)?;
        }
    }

    let body_len = u32::try_from(body.len()).map_err(|_| HuffError::PayloadLengthMismatch)?;
    let mut out = Vec::with_capacity(BODY_OFFSET + body.len());
    out.extend_from_slice(&original_len.to_le_bytes());
    out.extend_from_slice(&bits_total.to_le_bytes());
    out.extend_from_slice(&symbol_count.to_le_bytes());
    out.push(sole_symbol);
    out.push(0); // reserved
    out.extend_from_slice(&lengths);
    out.extend_from_slice(&body);
    debug_assert_eq!(out.len(), BODY_OFFSET + body_len as usize);
    Ok(out)
}

/// Parse result for one block payload.
#[derive(Debug)]
pub struct ParsedBlock<'a> {
    pub original_len: u32,
    pub bits_total: u32,
    pub symbol_count: u16,
    pub sole_symbol: u8,
    pub lengths: [u8; 256],
    pub body: &'a [u8],
}

impl<'a> ParsedBlock<'a> {
    /// Structural parse of a payload slice exactly matching `payload_len`.
    pub fn parse(payload: &'a [u8]) -> Result<Self> {
        if payload.len() < BODY_OFFSET {
            return Err(HuffError::PayloadTruncated);
        }
        let original_len = u32::from_le_bytes(payload[0..4].try_into().unwrap());
        let bits_total = u32::from_le_bytes(payload[4..8].try_into().unwrap());
        let symbol_count = u16::from_le_bytes(payload[8..10].try_into().unwrap());
        let sole_symbol = payload[10];
        let reserved = payload[11];
        if reserved != 0 {
            return Err(HuffError::UnknownFlags);
        }
        let mut lengths = [0u8; 256];
        lengths.copy_from_slice(&payload[PAYLOAD_HEADER_LEN..BODY_OFFSET]);
        let body = &payload[BODY_OFFSET..];

        // bits_total must not reach past the declared body bytes.
        if (bits_total as u64) > body.len() as u64 * 8 {
            return Err(HuffError::BitstreamTruncated);
        }

        Ok(Self { original_len, bits_total, symbol_count, sole_symbol, lengths, body })
    }

    /// Rebuild and bind the validated code table.
    pub fn table(&self) -> Result<CanonTable> {
        let table = CanonTable::validate(&self.lengths, self.symbol_count)?;
        if table.needs_convention_symbol() {
            return table.with_convention_symbol(self.sole_symbol);
        }
        Ok(table)
    }

    /// Decode the block, enforcing alphabet shape, padding, bitstream end and
    /// exact output length. Never reads or emits past declared limits.
    pub fn decode(&self) -> Result<Vec<u8>> {
        let table = self.table()?;

        // Empty block: only the empty alphabet with zero length and no bits.
        if self.original_len == 0 {
            if !table.is_empty_alphabet() || self.bits_total != 0 || !self.body.is_empty() {
                return Err(HuffError::OutputLengthMismatch);
            }
            return Ok(Vec::new());
        }

        // Non-empty input cannot use the empty alphabet.
        if table.is_empty_alphabet() {
            return Err(HuffError::TableSymbolShapeInvalid);
        }

        let mut out = Vec::with_capacity(self.original_len.min(1 << 20) as usize);
        let mut reader = BitReader::new(self.body, self.bits_total as u64)?;

        if let Some(symbol) = table.convention_symbol() {
            // One-symbol convention: exactly original_len copies, no bits.
            if self.bits_total != 0 {
                return Err(HuffError::InvalidPadding);
            }
            for _ in 0..self.original_len {
                out.push(symbol);
            }
        } else {
            for _ in 0..self.original_len {
                let sym = table.decode_symbol(&mut reader)?;
                out.push(sym);
            }
            // Every declared bit must belong to a consumed codeword.
            reader.expect_exhausted()?;
        }

        check_zero_padding(self.body, self.bits_total as u64)?;

        if out.len() as u32 != self.original_len {
            // Defensive: the loop above is bounded, but never trust an
            // intermediate state as success.
            return Err(HuffError::OutputLengthMismatch);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(data: &[u8]) {
        let payload = encode_block(data).unwrap();
        let parsed = ParsedBlock::parse(&payload).unwrap();
        let out = parsed.decode().unwrap();
        assert_eq!(out, data, "roundtrip mismatch");
    }

    #[test]
    fn empty_block_shape() {
        let payload = encode_block(b"").unwrap();
        assert_eq!(payload.len(), BODY_OFFSET);
        let parsed = ParsedBlock::parse(&payload).unwrap();
        assert_eq!(parsed.original_len, 0);
        assert_eq!(parsed.symbol_count, 0);
        assert_eq!(parsed.bits_total, 0);
        assert!(parsed.decode().unwrap().is_empty());
    }

    #[test]
    fn single_symbol_runs() {
        for len in [1usize, 2, 1000] {
            let data = vec![b'Z'; len];
            let payload = encode_block(&data).unwrap();
            let parsed = ParsedBlock::parse(&payload).unwrap();
            assert_eq!(parsed.symbol_count, 1);
            assert_eq!(parsed.sole_symbol, b'Z');
            assert_eq!(parsed.bits_total, 0);
            let out = parsed.decode().unwrap();
            assert_eq!(out, data);
        }
    }

    #[test]
    fn text_roundtrip() {
        roundtrip(b"the quick brown fox jumps over the lazy dog");
    }

    #[test]
    fn all_bytes_roundtrip() {
        let data: Vec<u8> = (0u16..256).map(|b| b as u8).collect();
        roundtrip(&data);
    }

    #[test]
    fn bad_padding_bits_are_rejected() {
        let mut payload = encode_block(b"canonical huffman padding probe").unwrap();
        let parsed = ParsedBlock::parse(&payload).unwrap();
        let valid_bits = parsed.bits_total;
        // Set the first padding bit to one.
        let last = payload.len() - 1;
        payload[last] |= 1u8 << (7 - (valid_bits & 7));
        let parsed = ParsedBlock::parse(&payload).unwrap();
        // Only relevant when the block actually has padding; otherwise the
        // mutation hits a payload bit and manifests as a decode failure.
        let result = parsed.decode();
        assert!(result.is_err(), "expected a decode failure, got {result:?}");
    }

    #[test]
    fn truncated_codeword_is_rejected() {
        let payload = encode_block(b"another truncation probe payload!!").unwrap();
        let mut parsed = ParsedBlock::parse(&payload).unwrap();
        let valid_bits = parsed.bits_total;
        if valid_bits < 4 {
            return; // guaranteed by chosen input
        }
        parsed.bits_total = valid_bits - 4;
        let err = parsed.decode().unwrap_err();
        assert!(
            err == HuffError::TruncatedCodeword || err == HuffError::InvalidPadding,
            "unexpected error kind: {err:?}"
        );
    }

    #[test]
    fn output_length_mismatch_detected() {
        let mut payload = encode_block(b"length mismatch probe").unwrap();
        // Inflate declared original length without changing the bitstream.
        let parsed_len = u32::from_le_bytes(payload[0..4].try_into().unwrap()) + 3;
        payload[0..4].copy_from_slice(&parsed_len.to_le_bytes());
        let parsed = ParsedBlock::parse(&payload).unwrap();
        assert_eq!(parsed.decode().unwrap_err(), HuffError::TruncatedCodeword);
    }
}
