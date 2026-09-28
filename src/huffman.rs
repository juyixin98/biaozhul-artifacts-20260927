//! Huffman tree construction and canonical code derivation.
//!
//! # The equal-frequency tie-break rule (normative)
//!
//! Tree construction uses a min-priority queue keyed on, in order:
//!
//! 1. **weight** — less frequent sub-trees merge first (standard Huffman);
//! 2. **tree height** — when weights are equal, the *shallower* tree is popped
//!    first, so weight ties keep the depth profile balanced instead of growing
//!    an unbalanced chain (e.g. weights `1,1,1,2` yield four length-2 codes,
//!    not lengths `1,2,3,3`);
//! 3. **minimum leaf symbol** — the tree containing the smallest symbol value
//!    wins as the final deterministic discriminator.
//!
//! All three key components are fixed by the input frequencies and symbol
//! values, so [`build_lengths`] is a pure deterministic function: every
//! conforming implementation must produce identical code lengths on the same
//! frequency table. The independent Python oracle in
//! `tests/golden/generate_fixtures.py` implements the same rule and its
//! outputs are asserted byte-for-byte in `tests/golden.rs`.
//!
//! # Canonical codes
//!
//! Once lengths are known, code words are derived in canonical form: sort
//! `(symbol, length)` by `(length asc, symbol asc)` and walk the standard
//! counter-and-shift recurrence, emitting bits MSB-first. The wire format
//! stores only `(symbol, length)` pairs, never code words themselves.

use crate::error::{Error, ErrorKind, Result};
use std::collections::BinaryHeap;

/// Maximum permitted code length in bits (Kraft math uses `u128`, so 32 is
/// safely representable, and length 32 itself is allowed).
pub const MAX_CODE_BITS: u8 = 32;

/// Number of distinct byte symbols in the alphabet.
pub const ALPHABET_SIZE: usize = 256;

/// A symbol with its assigned code length.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SymbolLength {
    /// Byte value this code describes.
    pub symbol: u8,
    /// Code length in bits (0 is reserved for the lone-symbol alphabet).
    pub length: u8,
}

/// Arena node for Huffman tree construction.
#[derive(Debug, Clone)]
enum Node {
    Leaf(u16),
    Internal { left: usize, right: usize },
}

/// Per-node metadata used as the heap key and for depth-independent merging.
#[derive(Debug, Clone)]
struct Meta {
    weight: u64,
    height: u32,
    min_symbol: u16,
}

/// Priority-queue entry. `Reverse((weight, height, min_symbol))` turns
/// `BinaryHeap` (a max-heap) into the required min-priority queue.
#[derive(Debug, Clone, PartialEq, Eq)]
struct HeapEntry(std::cmp::Reverse<(u64, u32, u16)>, usize);

impl PartialOrd for HeapEntry {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for HeapEntry {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // Ordering is entirely the Reverse tuple; the node index must never
        // influence priority (it used to here, which silently broke the
        // height/min-symbol tie-break).
        self.0.cmp(&other.0)
    }
}

/// Count symbol frequencies in `input`.
pub fn frequencies(input: &[u8]) -> [u64; ALPHABET_SIZE] {
    let mut freq = [0u64; ALPHABET_SIZE];
    for &b in input {
        freq[b as usize] += 1;
    }
    freq
}

/// Build code lengths for every symbol with a positive frequency.
///
/// Returns `(symbol, length)` pairs sorted by `(length, symbol)`, the order
/// canonical code assignment needs.
///
/// Special cases:
/// * no present symbol (empty input) → empty table;
/// * exactly one present symbol → the single `(symbol, 0)` pair; zero is the
///   reserved "no code word needed; expand by original length" length.
pub fn build_lengths(freq: &[u64; ALPHABET_SIZE]) -> Result<Vec<SymbolLength>> {
    let present: Vec<u16> = (0..ALPHABET_SIZE as u16)
        .filter(|&s| freq[s as usize] > 0)
        .collect();

    if present.is_empty() {
        return Ok(Vec::new());
    }
    if present.len() == 1 {
        return Ok(vec![SymbolLength {
            symbol: present[0] as u8,
            length: 0,
        }]);
    }

    let cap = 2 * present.len() - 1;
    let mut nodes: Vec<Node> = Vec::with_capacity(cap);
    let mut meta: Vec<Meta> = Vec::with_capacity(cap);
    let mut heap: BinaryHeap<HeapEntry> = BinaryHeap::with_capacity(present.len());

    for s in &present {
        let idx = nodes.len();
        nodes.push(Node::Leaf(*s));
        meta.push(Meta {
            weight: freq[*s as usize],
            height: 0,
            min_symbol: *s,
        });
        heap.push(HeapEntry(
            std::cmp::Reverse((freq[*s as usize], 0, *s)),
            idx,
        ));
    }

    while heap.len() > 1 {
        let HeapEntry(std::cmp::Reverse(k1), a) = heap.pop().expect("heap non-empty");
        let HeapEntry(std::cmp::Reverse(_k2), b) = heap.pop().expect("two pops available");
        debug_assert_eq!(k1, (meta[a].weight, meta[a].height, meta[a].min_symbol));

        let idx = nodes.len();
        nodes.push(Node::Internal { left: a, right: b });
        meta.push(Meta {
            weight: meta[a].weight + meta[b].weight,
            height: 1 + meta[a].height.max(meta[b].height),
            min_symbol: meta[a].min_symbol.min(meta[b].min_symbol),
        });
        heap.push(HeapEntry(
            std::cmp::Reverse((meta[idx].weight, meta[idx].height, meta[idx].min_symbol)),
            idx,
        ));
    }

    let root = heap.pop().expect("two or more leaves give a root").1;

    // Iterative depth walk; reject any tree exceeding the format limit.
    let mut depths = vec![0u8; nodes.len()];
    let mut stack = vec![(root, 0u8)];
    while let Some((idx, depth)) = stack.pop() {
        match &nodes[idx] {
            Node::Leaf(_) => depths[idx] = depth,
            Node::Internal { left, right } => {
                let next = depth + 1;
                if next > MAX_CODE_BITS {
                    return Err(Error::new(
                        ErrorKind::CodeLengthTooLong,
                        format!("optimal code length {next} exceeds format limit {MAX_CODE_BITS}"),
                    ));
                }
                stack.push((*left, next));
                stack.push((*right, next));
            }
        }
    }

    let mut result: Vec<SymbolLength> = Vec::with_capacity(present.len());
    for (i, node) in nodes.iter().enumerate() {
        if let Node::Leaf(sym) = node {
            result.push(SymbolLength {
                symbol: *sym as u8,
                length: depths[i],
            });
        }
    }
    // Canonical order: length first, then symbol.
    result.sort_unstable_by(|a, b| a.length.cmp(&b.length).then(a.symbol.cmp(&b.symbol)));
    Ok(result)
}

/// Derive canonical code words for a table sorted by `(length, symbol)`.
///
/// Standard recurrence: `code = (code + 1) << (len[i] - len[i-1])`.
/// Returned codes are MSB-aligned within a `u32`: the first bit to transmit
/// is bit 31, so a code of length `l` occupies bits `31 ..= 32-l`.
pub fn canonical_codes(sorted: &[SymbolLength]) -> Result<Vec<u32>> {
    if sorted.is_empty() {
        return Ok(Vec::new());
    }
    if sorted[0].length == 0 {
        if sorted.len() != 1 {
            return Err(Error::new(
                ErrorKind::ZeroLengthInMultiSymbolTable,
                "code length 0 is reserved for the lone-symbol alphabet",
            ));
        }
        return Ok(vec![0]);
    }

    let mut codes = Vec::with_capacity(sorted.len());
    let mut code: u32 = 0;
    let mut prev_len = sorted[0].length;
    for (i, sl) in sorted.iter().enumerate() {
        if i > 0 {
            let incremented = code.checked_add(1).ok_or_else(|| {
                Error::new(
                    ErrorKind::CodeLengthTooLong,
                    "canonical code counter overflowed u32",
                )
            })?;
            let shift = (sl.length - prev_len) as u32;
            code = incremented.checked_shl(shift).ok_or_else(|| {
                Error::new(
                    ErrorKind::CodeLengthTooLong,
                    format!("canonical code shift by {shift} overflowed"),
                )
            })?;
        }
        if sl.length < 32 && code >= (1u32 << sl.length) {
            return Err(Error::new(
                ErrorKind::OverSubscribedTree,
                format!("code {code:#x} does not fit in {} bits", sl.length),
            ));
        }
        codes.push(code << (32 - sl.length as u32));
        prev_len = sl.length;
    }
    Ok(codes)
}

/// Kraft equality check scaled by `2^max_len`: a complete prefix code
/// satisfies `sum(2^(max - len)) == 2^max`.
///
/// Returns `(scaled_sum, max_len)`; `max_len == 0` for an empty iterator.
pub fn kraft_scaled(lengths: impl Iterator<Item = u8>) -> (u128, u8) {
    let collected: Vec<u8> = lengths.collect();
    let max = collected.iter().copied().fold(0u8, u8::max);
    let sum = collected
        .iter()
        .fold(0u128, |acc, &l| acc + (1u128 << (max - l)));
    (sum, max)
}

/// Validate that `lengths` (an alphabet of `n >= 2` symbols) form a complete
/// prefix code with every length in `1..=MAX_CODE_BITS`. Both over-subscribed
/// (codes would collide) and incomplete (unused codes hide ambiguity) tables
/// are rejected as [`ErrorKind::OverSubscribedTree`].
pub fn validate_complete(lengths: &[SymbolLength]) -> Result<()> {
    if lengths.len() < 2 {
        return Err(Error::new(
            ErrorKind::OverSubscribedTree,
            "validate_complete requires at least two symbols",
        ));
    }
    for sl in lengths {
        if sl.length == 0 {
            return Err(Error::new(
                ErrorKind::ZeroLengthInMultiSymbolTable,
                format!(
                    "symbol {} has zero length in a multi-symbol table",
                    sl.symbol
                ),
            ));
        }
        if sl.length > MAX_CODE_BITS {
            return Err(Error::new(
                ErrorKind::CodeLengthTooLong,
                format!(
                    "symbol {} length {} exceeds {MAX_CODE_BITS}",
                    sl.symbol, sl.length
                ),
            ));
        }
    }
    let (sum, max) = kraft_scaled(lengths.iter().map(|sl| sl.length));
    let target = 1u128 << max;
    if sum > target {
        return Err(Error::new(
            ErrorKind::OverSubscribedTree,
            format!("Kraft sum {sum} > {target}: code words overlap"),
        ));
    }
    if sum < target {
        return Err(Error::new(
            ErrorKind::OverSubscribedTree,
            format!("Kraft sum {sum} < {target}: incomplete code tree"),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn freqs_from(pairs: &[(u8, u64)]) -> [u64; ALPHABET_SIZE] {
        let mut f = [0u64; ALPHABET_SIZE];
        for (s, c) in pairs {
            f[*s as usize] = *c;
        }
        f
    }

    fn lengths_of(t: &[SymbolLength]) -> Vec<(u8, u8)> {
        let mut v: Vec<(u8, u8)> = t.iter().map(|sl| (sl.symbol, sl.length)).collect();
        v.sort_unstable_by_key(|(s, _)| *s);
        v
    }

    #[test]
    fn empty_alphabet() {
        let f = [0u64; ALPHABET_SIZE];
        assert!(build_lengths(&f).unwrap().is_empty());
    }

    #[test]
    fn lone_symbol_gets_reserved_zero_length() {
        let f = freqs_from(&[(b'x', 42)]);
        let t = build_lengths(&f).unwrap();
        assert_eq!(
            t,
            vec![SymbolLength {
                symbol: b'x',
                length: 0
            }]
        );
        assert_eq!(canonical_codes(&t).unwrap(), vec![0]);
    }

    #[test]
    fn two_symbols_are_one_bit_each() {
        let f = freqs_from(&[(0, 1), (255, 1)]);
        let t = build_lengths(&f).unwrap();
        assert_eq!(lengths_of(&t), vec![(0, 1), (255, 1)]);
        let codes = canonical_codes(&t).unwrap();
        assert_eq!(codes, vec![0x0000_0000, 0x8000_0000]);
    }

    /// THE tie-break discriminating case: weights (1,1,1,2) on symbols
    /// (0,1,2,3). After merging symbols 0+1 (weight 2, height 1), the weight-2
    /// tie is between leaf 3 (height 0) and the internal node (height 1).
    /// The height-first rule pops the leaf, so the tree is balanced and every
    /// symbol gets length 2. A min-symbol-only implementation would assign
    /// lengths (3,3,2,1) instead.
    #[test]
    fn height_tie_break_balances_one_one_one_two() {
        let f = freqs_from(&[(0, 1), (1, 1), (2, 1), (3, 2)]);
        let t = build_lengths(&f).unwrap();
        validate_complete(&t).unwrap();
        assert_eq!(lengths_of(&t), vec![(0, 2), (1, 2), (2, 2), (3, 2)]);
        let codes = canonical_codes(&t).unwrap();
        assert_eq!(
            codes,
            vec![0x0000_0000, 0x4000_0000, 0x8000_0000, 0xC000_0000]
        );
    }

    /// When weight and height tie, min-symbol decides: four equal symbols.
    /// Merge (0,1), (2,3), then internals (min 0 vs min 2); all length 2.
    #[test]
    fn min_symbol_tie_four_equal_symbols() {
        let f = freqs_from(&[(0, 1), (1, 1), (2, 1), (3, 1)]);
        let t = build_lengths(&f).unwrap();
        assert_eq!(lengths_of(&t), vec![(0, 2), (1, 2), (2, 2), (3, 2)]);
    }

    /// Frequencies 45,13,12,16,9,5 on symbols 0..=5.
    /// Merge trace: 5+9=14, 12+13=25, 14+16=30, 25+30=55, 45+55=100.
    /// Lengths: s0=1; s1,s2,s3=3; s4,s5=4.
    /// Canonical codes (sorted by length then symbol):
    ///   s0=0; s1=100; s2=101; s3=110; s4=1110; s5=1111
    #[test]
    fn classic_profile_exact_lengths_and_codes() {
        let f = freqs_from(&[(0, 45), (1, 13), (2, 12), (3, 16), (4, 9), (5, 5)]);
        let t = build_lengths(&f).unwrap();
        validate_complete(&t).unwrap();
        assert_eq!(
            lengths_of(&t),
            vec![(0, 1), (1, 3), (2, 3), (3, 3), (4, 4), (5, 4)]
        );
        let codes = canonical_codes(&t).unwrap();
        // t is ordered (s0,1),(s1,3),(s2,3),(s3,3),(s4,4),(s5,4)
        assert_eq!(
            codes,
            vec![
                0x0000_0000, // 0
                0x8000_0000, // 100  MSB-aligned
                0xA000_0000, // 101
                0xC000_0000, // 110
                0xE000_0000, // 1110
                0xF000_0000, // 1111
            ]
        );
    }

    /// Height tie where the lower-height candidate is the later symbol:
    /// freqs (1,2,3,3,3). Merge 1+2=I01 (h1,min0,weight3); weight-3 ties are
    /// then leaf s2 (h0), leaf s3 (h0), leaf s4 (h0), I01 (h1): leaves pop
    /// first. Deterministic result: s0,s1 length 3; s2,s3,s4 length 2.
    #[test]
    fn height_before_min_symbol_with_internal_holding_smallest_symbol() {
        let f = freqs_from(&[(0, 1), (1, 2), (2, 3), (3, 3), (4, 3)]);
        let t = build_lengths(&f).unwrap();
        validate_complete(&t).unwrap();
        assert_eq!(lengths_of(&t), vec![(0, 3), (1, 3), (2, 2), (3, 2), (4, 2)]);
        let weighted: u64 = t
            .iter()
            .map(|sl| f[sl.symbol as usize] * sl.length as u64)
            .sum();
        // Merge costs 3+6+6+12 = 27.
        assert_eq!(weighted, 27);
    }

    #[test]
    fn all_256_symbols_complete() {
        let freq = [1u64; ALPHABET_SIZE];
        let t = build_lengths(&freq).unwrap();
        assert_eq!(t.len(), 256);
        validate_complete(&t).unwrap();
        // 256 equiprobable symbols all get length 8.
        assert!(t.iter().all(|sl| sl.length == 8));
    }

    #[test]
    fn over_subscribed_length_set_rejected() {
        // Three 1-bit codes: Kraft 3/2 > 1.
        let t = vec![
            SymbolLength {
                symbol: 0,
                length: 1,
            },
            SymbolLength {
                symbol: 1,
                length: 1,
            },
            SymbolLength {
                symbol: 2,
                length: 1,
            },
        ];
        assert_eq!(
            validate_complete(&t).unwrap_err().kind(),
            ErrorKind::OverSubscribedTree
        );
    }

    #[test]
    fn incomplete_tree_rejected() {
        // Two symbols with lengths (1,2): Kraft 1/2 + 1/4 = 3/4 < 1.
        let t = vec![
            SymbolLength {
                symbol: 7,
                length: 1,
            },
            SymbolLength {
                symbol: 9,
                length: 2,
            },
        ];
        assert_eq!(
            validate_complete(&t).unwrap_err().kind(),
            ErrorKind::OverSubscribedTree
        );
    }

    #[test]
    fn zero_length_in_multi_symbol_table_rejected() {
        let t = vec![
            SymbolLength {
                symbol: 1,
                length: 0,
            },
            SymbolLength {
                symbol: 2,
                length: 1,
            },
        ];
        assert_eq!(
            canonical_codes(&t).unwrap_err().kind(),
            ErrorKind::ZeroLengthInMultiSymbolTable
        );
        assert_eq!(
            validate_complete(&t).unwrap_err().kind(),
            ErrorKind::ZeroLengthInMultiSymbolTable
        );
    }
}
