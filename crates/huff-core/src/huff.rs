//! Canonical Huffman code construction and validation (FORMAT.md §3–§4).
//!
//! # Deterministic tie-break rule (normative)
//!
//! Frequencies are gathered over the 256-symbol byte alphabet. When more than
//! one tree node shares the smallest weight, the priority queue breaks ties in
//! this exact order:
//!
//! 1. **weight** ascending,
//! 2. **height** ascending (a leaf has height 0),
//! 3. **node id** ascending — leaves are inserted in ascending symbol order
//!    (ids `0..=255` by symbol value); internal nodes receive ids in the order
//!    they are created (`256, 257, …`).
//!
//! The two smallest entries are always popped together. Once the depths are
//! fixed, the code table is canonical: symbols are ordered by
//! `(code length asc, symbol asc)`, codes start at 0 and are shifted left when
//! the length grows. A one-symbol alphabet uses the length-0 convention and
//! emits no bits; the zero-symbol alphabet is reserved for empty blocks
//! (handled by [`crate::block`]).

use std::cmp::Reverse;
use std::collections::BinaryHeap;

use crate::bits::BitWriter;
use crate::error::{HuffError, Result};
use crate::MAX_CODE_LEN;

/// Encoder-side canonical codebook.
#[derive(Debug, Clone)]
pub struct Codebook {
    /// Code length per byte symbol (0 = absent).
    pub lengths: [u8; 256],
    /// Canonical code value per symbol; the low `lengths[s]` bits are used.
    pub codes: [u32; 256],
    /// Symbols present in the alphabet, ascending.
    pub present: Vec<u8>,
}

impl Codebook {
    /// Build the canonical codebook for exact symbol frequencies.
    ///
    /// * `present.len() == 0` is rejected here; empty blocks are encoded by
    ///   [`crate::block`] without calling this function.
    /// * `present.len() == 1` yields the length-0 one-symbol convention.
    pub fn from_frequencies(freq: &[u64; 256]) -> Result<Self> {
        let present: Vec<u8> =
            (0u16..256).filter(|&s| freq[s as usize] > 0).map(|s| s as u8).collect();
        let n = present.len();
        let mut lengths = [0u8; 256];

        if n == 0 {
            return Err(HuffError::BadSymbolCount);
        }
        if n == 1 {
            // Length-0 convention; the sole symbol is stored by the block.
            return Ok(Self { lengths, codes: [0u32; 256], present });
        }

        // Dense node array: leaves occupy ids in ascending-symbol order,
        // internal nodes follow in creation order.
        let total_nodes = 2 * n - 1;
        let mut left = vec![u16::MAX; total_nodes];
        let mut right = vec![u16::MAX; total_nodes];
        let mut height = vec![0u16; total_nodes];
        let mut weight = vec![0u64; total_nodes];
        for (i, &sym) in present.iter().enumerate() {
            weight[i] = freq[sym as usize];
        }

        // BinaryHeap is a max-heap; Reverse makes the smallest tuple win.
        // Tuple order: (weight, height, id) — the normative tie-break above.
        let mut heap: BinaryHeap<Reverse<(u64, u16, usize)>> =
            BinaryHeap::with_capacity(total_nodes);
        for i in 0..n {
            heap.push(Reverse((weight[i], 0, i)));
        }

        let mut next_id = n;
        while heap.len() > 1 {
            let Reverse((_, _, id1)) = heap.pop().unwrap();
            let Reverse((_, _, id2)) = heap.pop().unwrap();
            let id = next_id;
            next_id += 1;
            weight[id] = weight[id1] + weight[id2];
            height[id] = height[id1].max(height[id2]) + 1;
            left[id] = id1 as u16;
            right[id] = id2 as u16;
            heap.push(Reverse((weight[id], height[id], id)));
        }
        let root = next_id - 1;

        // Depth assignment via iterative DFS (avoids recursion depth concerns).
        let mut stack = vec![(root as u16, 0u8)];
        while let Some((node, depth)) = stack.pop() {
            let node = node as usize;
            if left[node] == u16::MAX {
                // Leaf: dense leaf id i corresponds to present[i].
                lengths[present[node] as usize] = depth;
            } else {
                if depth >= MAX_CODE_LEN {
                    // Children would sit at depth MAX_CODE_LEN+1.
                    return Err(HuffError::CodeLenTooLong);
                }
                stack.push((right[node], depth + 1));
                stack.push((left[node], depth + 1));
            }
        }

        let mut codes = [0u32; 256];
        assign_canonical(&lengths, &present, &mut codes);
        Ok(Self { lengths, codes, present })
    }

    /// Encode one symbol into the writer.
    pub fn encode_symbol(&self, w: &mut BitWriter, symbol: u8) {
        let len = self.lengths[symbol as usize];
        if len > 0 {
            w.write_bits(self.codes[symbol as usize], len);
        }
        // One-symbol convention: no bits emitted.
    }
}

/// Assign canonical code values given depths. `present` is ascending.
fn assign_canonical(lengths: &[u8; 256], present: &[u8], codes: &mut [u32; 256]) {
    // Order by (length asc, symbol asc); present is already symbol-ascending.
    let mut order: Vec<u8> = present.to_vec();
    order.sort_by_key(|&s| (lengths[s as usize], s));

    let mut code: u32 = 0;
    let mut prev_len = 0u8;
    for s in order {
        let len = lengths[s as usize];
        if prev_len != 0 {
            code = (code + 1) << (len - prev_len);
        }
        codes[s as usize] = code;
        prev_len = len;
    }
}

/// Shape of a validated alphabet.
#[derive(Debug, Clone, PartialEq, Eq)]
enum TableShape {
    /// Empty alphabet; only legal for an empty block.
    Empty,
    /// One symbol with the length-0 convention. `u8::MAX` = not yet bound.
    OneSymbol(u8),
    /// Normal prefix alphabet (possibly incomplete).
    Prefix,
}

/// Decoder-side canonical table, rebuilt from a validated code-length set.
#[derive(Debug, Clone)]
pub struct CanonTable {
    shape: TableShape,
    /// Number of codes of each length, indexed 0..=MAX_CODE_LEN.
    counts: [u32; MAX_CODE_LEN as usize + 1],
    /// Smallest canonical code actually assigned at each length (valid only
    /// where `counts[len] > 0`).
    base: [u32; MAX_CODE_LEN as usize + 1],
    /// Start offset within `symbols` for each used length.
    offset: [u32; MAX_CODE_LEN as usize + 1],
    /// Symbols ordered by (length asc, symbol asc).
    symbols: Vec<u8>,
}

impl CanonTable {
    /// Validate a code-length set and rebuild the canonical table.
    ///
    /// Accepted shapes (see FORMAT.md §4):
    /// * `symbol_count == 0` and all lengths zero — empty alphabet, legal only
    ///   for empty blocks;
    /// * `symbol_count == 1` and all lengths zero — the one-symbol length-0
    ///   convention; the block header stores the sole symbol and
    ///   [`CanonTable::with_convention_symbol`] binds it;
    /// * `symbol_count >= 2` positive lengths satisfying the Kraft inequality.
    pub fn validate(lengths: &[u8; 256], symbol_count: u16) -> Result<Self> {
        if symbol_count > 256 {
            return Err(HuffError::BadSymbolCount);
        }
        const EMPTY_ARRAYS: [u32; MAX_CODE_LEN as usize + 1] = [0; MAX_CODE_LEN as usize + 1];
        let positive: Vec<u8> = (0u16..256)
            .filter(|&s| lengths[s as usize] > 0)
            .map(|s| s as u8)
            .collect();
        for &len in lengths.iter() {
            if len > MAX_CODE_LEN {
                return Err(HuffError::CodeLenTooLong);
            }
        }

        if symbol_count <= 1 {
            // Convention shapes require *all* lengths zero; any positive entry
            // with fewer than two symbols is an incomplete/ambiguous code.
            if !positive.is_empty() {
                return Err(HuffError::TableIncomplete);
            }
            let shape = if symbol_count == 0 {
                TableShape::Empty
            } else {
                TableShape::OneSymbol(u8::MAX) // bound by the block layer
            };
            return Ok(Self {
                shape,
                counts: EMPTY_ARRAYS,
                base: EMPTY_ARRAYS,
                offset: EMPTY_ARRAYS,
                symbols: Vec::new(),
            });
        }

        if positive.len() != symbol_count as usize {
            return Err(HuffError::BadSymbolCount);
        }

        // Kraft inequality in exact integer arithmetic:
        // sum 2^(MAX-l) must be <= 2^MAX.
        let mut kraft: u128 = 0;
        for &s in &positive {
            kraft += 1u128 << (MAX_CODE_LEN - lengths[s as usize]);
        }
        let full = 1u128 << MAX_CODE_LEN;
        if kraft > full {
            return Err(HuffError::TableKraftOverflow);
        }
        // Incomplete multi-symbol alphabets (kraft < full) are accepted, like
        // DEFLATE: unassigned prefixes fail at decode time as INVALID_CODEWORD.

        let mut counts = [0u32; MAX_CODE_LEN as usize + 1];
        for &s in &positive {
            counts[lengths[s as usize] as usize] += 1;
        }

        // Canonical code recurrence (INFLATE algorithm, RFC 1951 §3.2.2):
        // running code is shifted across *every* length, including empty
        // levels; the stored base is only meaningful for used lengths.
        let mut base = [0u32; MAX_CODE_LEN as usize + 1];
        let mut offset = [0u32; MAX_CODE_LEN as usize + 1];
        let mut symbols = Vec::with_capacity(positive.len());
        let mut running: u32 = 0;
        let mut off: u32 = 0;
        for len in 1..=MAX_CODE_LEN {
            running <<= 1;
            if counts[len as usize] > 0 {
                base[len as usize] = running;
                offset[len as usize] = off;
                let mut at_len: Vec<u8> = positive
                    .iter()
                    .copied()
                    .filter(|&s| lengths[s as usize] == len)
                    .collect();
                at_len.sort_unstable();
                symbols.extend(at_len);
                running += counts[len as usize];
                off += counts[len as usize];
            }
        }

        Ok(Self {
            shape: TableShape::Prefix,
            counts,
            base,
            offset,
            symbols,
        })
    }

    /// Bind the distinguished symbol to a validated one-symbol table.
    pub fn with_convention_symbol(mut self, symbol: u8) -> Result<Self> {
        match self.shape {
            TableShape::OneSymbol(_) => {
                self.shape = TableShape::OneSymbol(symbol);
                Ok(self)
            }
            _ => Err(HuffError::TableSymbolShapeInvalid),
        }
    }

    /// Build a one-symbol table for a known symbol.
    pub fn new_one_symbol(symbol: u8) -> Self {
        Self {
            shape: TableShape::OneSymbol(symbol),
            counts: [0; MAX_CODE_LEN as usize + 1],
            base: [0; MAX_CODE_LEN as usize + 1],
            offset: [0; MAX_CODE_LEN as usize + 1],
            symbols: Vec::new(),
        }
    }

    /// Sole symbol when the length-0 convention is used and bound.
    pub fn convention_symbol(&self) -> Option<u8> {
        match self.shape {
            TableShape::OneSymbol(s) if s != u8::MAX => Some(s),
            _ => None,
        }
    }

    /// Whether this is the empty alphabet (legal only for empty blocks).
    pub fn is_empty_alphabet(&self) -> bool {
        matches!(self.shape, TableShape::Empty)
    }

    /// Whether this table still needs its convention symbol bound.
    pub fn needs_convention_symbol(&self) -> bool {
        matches!(self.shape, TableShape::OneSymbol(u8::MAX))
    }

    /// Decode exactly one symbol, consuming its code bits.
    pub fn decode_symbol<R: BitInput>(&self, input: &mut R) -> Result<u8> {
        match self.shape {
            TableShape::OneSymbol(s) => Ok(s),
            TableShape::Empty => Err(HuffError::InvalidCodeword),
            TableShape::Prefix => {
                let mut code: u32 = 0;
                for len in 1u8..=MAX_CODE_LEN {
                    code = (code << 1) | input.read_bit()? as u32;
                    let count = self.counts[len as usize];
                    if count > 0 {
                        let first_code = self.base[len as usize];
                        if code >= first_code {
                            let idx = code - first_code;
                            if idx < count {
                                return Ok(self.symbols
                                    [(self.offset[len as usize] + idx) as usize]);
                            }
                        }
                    }
                }
                Err(HuffError::InvalidCodeword)
            }
        }
    }
}

/// Minimal bit source used by the decoder so tests can supply independent readers.
pub trait BitInput {
    fn read_bit(&mut self) -> Result<u8>;
}

impl<'a> BitInput for crate::bits::BitReader<'a> {
    fn read_bit(&mut self) -> Result<u8> {
        crate::bits::BitReader::read_bit(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bits::BitReader;

    fn freq_of(data: &[u8]) -> [u64; 256] {
        let mut f = [0u64; 256];
        for &b in data {
            f[b as usize] += 1;
        }
        f
    }

    #[test]
    fn two_symbol_codes_are_zero_and_one() {
        let mut f = [0u64; 256];
        f[0] = 7;
        f[255] = 3;
        let cb = Codebook::from_frequencies(&f).unwrap();
        assert_eq!(cb.lengths[0], 1);
        assert_eq!(cb.lengths[255], 1);
        // Symbol order tie: 0 gets code 0, 255 gets code 1.
        assert_eq!(cb.codes[0], 0);
        assert_eq!(cb.codes[255], 1);
    }

    #[test]
    fn tie_break_equal_frequencies_is_by_symbol() {
        // 4 symbols, equal weight: tree depth 2 for all; canonical codes
        // assigned in symbol order — symbol 10 must be 00.
        let mut f = [0u64; 256];
        for s in [10u8, 20, 30, 40] {
            f[s as usize] = 5;
        }
        let cb = Codebook::from_frequencies(&f).unwrap();
        for &s in &[10, 20, 30, 40] {
            assert_eq!(cb.lengths[s as usize], 2);
        }
        assert_eq!(cb.codes[10], 0b00);
        assert_eq!(cb.codes[20], 0b01);
        assert_eq!(cb.codes[30], 0b10);
        assert_eq!(cb.codes[40], 0b11);
    }

    #[test]
    fn one_symbol_zero_length_convention() {
        let mut f = [0u64; 256];
        f[b'x' as usize] = 42;
        let cb = Codebook::from_frequencies(&f).unwrap();
        assert_eq!(cb.lengths[b'x' as usize], 0);
        let mut w = BitWriter::new();
        cb.encode_symbol(&mut w, b'x');
        cb.encode_symbol(&mut w, b'x');
        let (bytes, bits) = w.finish();
        assert_eq!(bits, 0);
        assert!(bytes.is_empty());
    }

    #[test]
    fn kraft_overflow_is_rejected() {
        // Three length-1 codes: Kraft sum = 3/2 > 1.
        let mut lengths = [0u8; 256];
        lengths[0] = 1;
        lengths[1] = 1;
        lengths[2] = 1;
        assert_eq!(
            CanonTable::validate(&lengths, 3).unwrap_err(),
            HuffError::TableKraftOverflow
        );
    }

    #[test]
    fn one_symbol_positive_length_is_incomplete() {
        let mut lengths = [0u8; 256];
        lengths[7] = 1;
        assert_eq!(
            CanonTable::validate(&lengths, 1).unwrap_err(),
            HuffError::TableIncomplete
        );
    }

    #[test]
    fn roundtrip_decoder_reads_canonical_bits() {
        let data = b"abracadabra";
        let cb = Codebook::from_frequencies(&freq_of(data)).unwrap();
        let mut w = BitWriter::new();
        for &b in data {
            cb.encode_symbol(&mut w, b);
        }
        let (bytes, bits) = w.finish();

        let table = CanonTable::validate(&cb.lengths, cb.present.len() as u16).unwrap();
        let mut r = BitReader::new(&bytes, bits).unwrap();
        let mut out = Vec::new();
        for _ in data {
            out.push(table.decode_symbol(&mut r).unwrap());
        }
        assert_eq!(out, data);
    }

    #[test]
    fn invalid_prefix_is_detected() {
        // Complete 2-symbol table but an empty stream: reading fails truncated.
        let mut lengths = [0u8; 256];
        lengths[0] = 1;
        lengths[1] = 1;
        let table = CanonTable::validate(&lengths, 2).unwrap();
        let body: [u8; 0] = [];
        let mut r = BitReader::new(&body, 0).unwrap();
        assert_eq!(
            table.decode_symbol(&mut r).unwrap_err(),
            HuffError::TruncatedCodeword
        );
    }
}
