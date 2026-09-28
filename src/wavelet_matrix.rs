//! Wavelet matrix over `u64` ranks.
//!
//! The matrix is built over a fixed bit `height` from the most significant
//! bit down. Each level stores a [`BitVector`] of the current level bits
//! plus `zeros`, the number of 0-bits at that level (needed to map a
//! position in the 1-subspace back to the stable-partitioned sequence).

use crate::bitvector::BitVector;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Level {
    pub bv: BitVector,
    pub zeros: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaveletMatrix {
    height: usize,
    len: usize,
    /// `levels[d]` corresponds to bit position `height - 1 - d`.
    levels: Vec<Level>,
}

impl WaveletMatrix {
    /// Build from integer ranks.
    ///
    /// `height` must be large enough that every value fits in `height` bits,
    /// i.e. `values.iter().all(|&v| v < 2^height)`.
    pub fn build(values: &[u64], height: usize) -> Self {
        assert!((1..=64).contains(&height), "height must be in [1, 64]");
        let len = values.len();
        let mut cur = values.to_vec();
        let mut levels = Vec::with_capacity(height);

        for d in 0..height {
            let bit_level = height - 1 - d;
            let bit = 1u64.checked_shl(bit_level as u32);

            let mut bits = vec![false; cur.len()];
            let mut zeros = 0usize;
            for (i, &v) in cur.iter().enumerate() {
                let one = bit.map(|b| v & b != 0).unwrap_or(false);
                bits[i] = one;
                if !one {
                    zeros += 1;
                }
            }

            // Stable partition: 0-bits keep their order, then 1-bits.
            let mut next = Vec::with_capacity(cur.len());
            for &v in &cur {
                let one = bit.map(|b| v & b != 0).unwrap_or(false);
                if !one {
                    next.push(v);
                }
            }
            for &v in &cur {
                let one = bit.map(|b| v & b != 0).unwrap_or(false);
                if one {
                    next.push(v);
                }
            }

            levels.push(Level {
                bv: BitVector::from_bits(&bits),
                zeros,
            });
            cur = next;
        }

        WaveletMatrix {
            height,
            len,
            levels,
        }
    }

    /// Rebuild after deserialization; validates structural consistency.
    pub fn restore(height: usize, len: usize, levels: Vec<Level>) -> Result<Self, String> {
        if !(1..=64).contains(&height) {
            return Err(format!("height {height} outside [1, 64]"));
        }
        if levels.len() != height {
            return Err(format!("expected {height} levels, found {}", levels.len()));
        }
        for (d, lvl) in levels.iter().enumerate() {
            if lvl.bv.len() != len {
                return Err(format!("level {d} has length {} != {len}", lvl.bv.len()));
            }
            if lvl.zeros > len {
                return Err(format!("level {d} reports more zeros than length"));
            }
        }
        Ok(WaveletMatrix {
            height,
            len,
            levels,
        })
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn height(&self) -> usize {
        self.height
    }

    pub fn levels(&self) -> &[Level] {
        &self.levels
    }

    pub fn get(&self, mut pos: usize) -> u64 {
        debug_assert!(pos < self.len);
        let mut val = 0u64;
        for d in 0..self.height {
            let bit_level = self.height - 1 - d;
            let lvl = &self.levels[d];
            if lvl.bv.get(pos) {
                val |= 1u64.checked_shl(bit_level as u32).unwrap_or(1u64 << 63);
                pos = lvl.zeros + lvl.bv.rank1(pos);
            } else {
                pos = lvl.bv.rank0(pos);
            }
        }
        val
    }

    /// 0-based `k`-th smallest value in the half-open window `[l, r)`.
    ///
    /// Caller guarantees `l < r` and `k < r - l`.
    pub fn quantile(&self, mut l: usize, mut r: usize, mut k: usize) -> u64 {
        debug_assert!(l < r && r <= self.len);
        debug_assert!(k < r - l);
        let mut val = 0u64;
        for d in 0..self.height {
            let bit_level = self.height - 1 - d;
            let lvl = &self.levels[d];
            let l0 = lvl.bv.rank0(l);
            let r0 = lvl.bv.rank0(r);
            let zeros_in_window = r0 - l0;
            if k < zeros_in_window {
                // Answer lives in the 0-subspace.
                l = l0;
                r = r0;
            } else {
                // Answer lives in the 1-subspace; skip the zeros.
                k -= zeros_in_window;
                if let Some(b) = 1u64.checked_shl(bit_level as u32) {
                    val |= b;
                }
                l = lvl.zeros + lvl.bv.rank1(l);
                r = lvl.zeros + lvl.bv.rank1(r);
            }
        }
        val
    }

    /// Number of elements in `[l, r)` whose value is strictly below `upper`.
    ///
    /// Caller guarantees `l < r` and `r <= len`.
    pub fn range_freq(&self, mut l: usize, mut r: usize, upper: u64) -> usize {
        debug_assert!(l < r && r <= self.len);
        // Everything is below `upper` when `upper` covers the whole domain.
        // `1u128 << height` keeps this safe for height == 64.
        if upper as u128 >= 1u128 << self.height {
            return r - l;
        }
        let mut answer = 0usize;
        for d in 0..self.height {
            let bit_level = self.height - 1 - d;
            let lvl = &self.levels[d];
            let upper_bit_is_1 = (upper >> bit_level) & 1 == 1;
            if upper_bit_is_1 {
                // Everything with this bit 0 is strictly below `upper`;
                // count it and continue inside the 1-subspace.
                answer += lvl.bv.rank0(r) - lvl.bv.rank0(l);
                l = lvl.zeros + lvl.bv.rank1(l);
                r = lvl.zeros + lvl.bv.rank1(r);
            } else {
                // Continue inside the 0-subspace.
                l = lvl.bv.rank0(l);
                r = lvl.bv.rank0(r);
            }
        }
        answer
    }
}
