//! Rank / occurrence structure over the BWT.
//!
//! `occ(sym, i)` = number of occurrences of symbol `sym` in `bwt[0..i]`
//! (**exclusive** of position `i`).
//!
//! The structure stores the BWT symbols plus, every [`RANK_BLOCK`] rows, a
//! cumulative count snapshot for the full 257-symbol alphabet. A query is a
//! snapshot lookup plus a linear scan of at most `RANK_BLOCK - 1` symbols.
//! Memory is `n` bytes (packed symbols) plus `257 * ceil(n/B) * 8` bytes of
//! snapshots — the "rank structure" the FM index queries against.

use crate::coding::{ALPHABET_SIZE, SENTINEL};

/// Block (snapshot) granularity; must be `>= 2`.
pub const RANK_BLOCK: usize = 256;

#[derive(Debug, Clone)]
pub struct OccTable {
    /// BWT symbols packed into `u16` (sentinel or byte-derived 1..=256).
    bwt: Vec<u16>,
    /// `blocks[k][sym]` = occ(sym, k * RANK_BLOCK). Length = num_blocks + 1.
    blocks: Vec<[u64; ALPHABET_SIZE]>,
}

impl OccTable {
    /// Build the occurrence structure from BWT symbols.
    pub fn new(bwt: Vec<u16>) -> Self {
        let n = bwt.len();
        let num_snapshots = n / RANK_BLOCK + 1;
        let mut blocks = vec![[0u64; ALPHABET_SIZE]; num_snapshots + 1];
        let mut running = [0u64; ALPHABET_SIZE];
        for (i, &sym) in bwt.iter().enumerate() {
            if i % RANK_BLOCK == 0 {
                blocks[i / RANK_BLOCK] = running;
            }
            running[sym as usize] += 1;
        }
        blocks[num_snapshots] = running; // total counts
        Self { bwt, blocks }
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.bwt.len()
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.bwt.is_empty()
    }

    #[inline]
    pub fn symbol_at(&self, row: usize) -> u16 {
        self.bwt[row]
    }

    /// occ(sym, i): occurrences of `sym` in `bwt[0..i]`.
    ///
    /// Requires `i <= len()`. Out-of-range symbols or `i > len()` are a
    /// programming error and panic — the index core only ever calls this with
    /// validated symbols and intervals.
    pub fn occ(&self, sym: u16, i: usize) -> u64 {
        assert!((sym as usize) < ALPHABET_SIZE, "symbol out of alphabet");
        assert!(i <= self.bwt.len(), "occ endpoint past BWT");
        let block = i / RANK_BLOCK;
        let start = block * RANK_BLOCK;
        let mut count = self.blocks[block][sym as usize];
        for &s in &self.bwt[start..i] {
            if s == sym {
                count += 1;
            }
        }
        count
    }

    /// Total occurrences of `sym` across the whole BWT.
    pub fn total(&self, sym: u16) -> u64 {
        *self
            .blocks
            .last()
            .expect("OccTable always has a total snapshot")
            .get(sym as usize)
            .unwrap_or(&0)
    }

    /// Raw blocks for persistence / tests.
    pub fn blocks(&self) -> &[[u64; ALPHABET_SIZE]] {
        &self.blocks
    }

    pub fn bwt(&self) -> &[u16] {
        &self.bwt
    }

    /// Reconstruct from persisted parts and validate internal consistency.
    pub fn from_parts(bwt: Vec<u16>) -> Self {
        Self::new(bwt)
    }

    /// Debug check: exactly one sentinel sits in the BWT.
    pub fn debug_assert_well_formed(&self) {
        debug_assert_eq!(
            self.bwt.iter().filter(|&&s| s == SENTINEL).count(),
            1,
            "BWT must contain exactly one sentinel"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn brute_occ(bwt: &[u16], sym: u16, i: usize) -> u64 {
        bwt[..i].iter().filter(|&&s| s == sym).count() as u64
    }

    #[test]
    fn occ_matches_brute_force_at_every_boundary() {
        let bwt: Vec<u16> = (0..2000u32)
            .map(|i| match i % 11 {
                0 => SENTINEL,
                r => (r as u16) * 23,
            })
            .enumerate()
            // force the sentinel to appear exactly once at row 123
            .map(|(idx, s)| {
                if idx == 123 {
                    SENTINEL
                } else if s == SENTINEL {
                    7
                } else {
                    s
                }
            })
            .collect();
        let table = OccTable::new(bwt.clone());
        for i in 0..=bwt.len() {
            for sym in [SENTINEL, 1, 46, 230, 256] {
                assert_eq!(
                    table.occ(sym, i),
                    brute_occ(&bwt, sym, i),
                    "occ({sym}, {i}) mismatch"
                );
            }
        }
        // endpoints and block boundaries
        assert_eq!(table.occ(SENTINEL, 0), 0);
        assert_eq!(table.occ(SENTINEL, bwt.len()), 1);
        assert_eq!(table.occ(7, 0), 0);
    }

    #[test]
    fn occ_empty_bwt_is_defined() {
        let table = OccTable::new(vec![]);
        assert_eq!(table.len(), 0);
        assert_eq!(table.occ(SENTINEL, 0), 0);
    }
}
