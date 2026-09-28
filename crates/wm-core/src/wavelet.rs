//! Wavelet matrix over order-compressed `i64` sequences.
//!
//! Compression maps the j-th smallest *distinct* value to id `j`. Duplicates
//! share one id; signed extremes (`i64::MIN`/`i64::MAX`) need no special
//! handling because compression works on the native `Ord` of `i64`.
//!
//! Levels are stored from most significant id bit to least significant.
//! Each query is a single top-down navigation using only `rank0`/`rank1`.

use std::collections::BTreeSet;

use crate::bitvec::BitVector;
use crate::error::WmError;

/// Immutable wavelet matrix index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaveletMatrix {
    /// Levels in MSB -> LSB order; `levels.len() == bit_len`.
    levels: Vec<BitVector>,
    /// Number of zero bits at each level.
    zero_counts: Vec<usize>,
    /// Bit width of compressed ids.
    bit_len: usize,
    /// Distinct original values in ascending order; id is the index.
    values: Vec<i64>,
    n: usize,
}

impl WaveletMatrix {
    /// Build an index from a non-empty value sequence.
    ///
    /// # Errors
    /// [`WmError::EmptyValues`] when `values` is empty.
    pub fn build(values: &[i64]) -> Result<Self, WmError> {
        if values.is_empty() {
            return Err(WmError::EmptyValues);
        }
        let distinct: Vec<i64> = values
            .iter()
            .copied()
            .collect::<BTreeSet<i64>>()
            .into_iter()
            .collect();
        let max_id = (distinct.len() - 1) as u64;
        let bit_len = if max_id == 0 {
            0
        } else {
            64 - max_id.leading_zeros() as usize
        };

        // Current permutation of ids.
        let mut cur: Vec<u64> = values
            .iter()
            .map(|v| {
                // Infallible: every value comes from `distinct`.
                distinct.binary_search(v).unwrap() as u64
            })
            .collect();

        let mut levels = Vec::with_capacity(bit_len);
        let mut zero_counts = Vec::with_capacity(bit_len);

        for level in (0..bit_len).rev() {
            let bits: Vec<bool> = cur.iter().map(|id| (id >> level) & 1 == 1).collect();
            let bv = BitVector::from_bools(&bits);
            let zeros = cur.len() - bv.rank1(cur.len());
            let mut next = Vec::with_capacity(cur.len());
            // Stable partition: zeros first, then ones.
            for id in &cur {
                if (*id >> level) & 1 == 0 {
                    next.push(*id);
                }
            }
            for id in &cur {
                if (*id >> level) & 1 == 1 {
                    next.push(*id);
                }
            }
            levels.push(bv);
            zero_counts.push(zeros);
            cur = next;
        }

        Ok(Self {
            levels,
            zero_counts,
            bit_len,
            values: distinct,
            n: values.len(),
        })
    }

    /// Reconstruct from validated parts (used by the persistence layer).
    ///
    /// Callers are expected to verify structural consistency; this
    /// constructor re-validates defensively.
    ///
    /// # Errors
    /// Returns an error describing the first structural inconsistency found.
    pub fn from_parts(
        levels: Vec<BitVector>,
        zero_counts: Vec<usize>,
        values: Vec<i64>,
        n: usize,
    ) -> Result<Self, InvalidParts> {
        if n == 0 {
            return Err(InvalidParts::EmptyIndex);
        }
        if values.is_empty() {
            return Err(InvalidParts::EmptyDistinct);
        }
        for w in values.windows(2) {
            if w[0] >= w[1] {
                return Err(InvalidParts::DistinctNotSorted);
            }
        }
        if levels.len() != zero_counts.len() {
            return Err(InvalidParts::LevelCountMismatch {
                levels: levels.len(),
                zeros: zero_counts.len(),
            });
        }
        let expected_bits = {
            let max_id = (values.len() - 1) as u64;
            if max_id == 0 {
                0
            } else {
                64 - max_id.leading_zeros() as usize
            }
        };
        if levels.len() != expected_bits {
            return Err(InvalidParts::UnexpectedBitWidth {
                levels: levels.len(),
                expected: expected_bits,
            });
        }
        for (i, lvl) in levels.iter().enumerate() {
            if lvl.len() != n {
                return Err(InvalidParts::LevelLength {
                    level: i,
                    len: lvl.len(),
                    expected: n,
                });
            }
            let ones = lvl.rank1(n);
            if zero_counts[i] != n - ones {
                return Err(InvalidParts::ZeroCount {
                    level: i,
                    stored: zero_counts[i],
                    expected: n - ones,
                });
            }
        }
        Ok(Self {
            levels,
            zero_counts,
            bit_len: expected_bits,
            values,
            n,
        })
    }

    /// Sequence length.
    #[must_use]
    pub fn len(&self) -> usize {
        self.n
    }

    /// Indexes are never empty; always false (kept for API symmetry).
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.n == 0
    }

    /// Number of distinct values.
    #[must_use]
    pub fn distinct_count(&self) -> usize {
        self.values.len()
    }

    /// Bit width of the compressed ids.
    #[must_use]
    pub fn bit_len(&self) -> usize {
        self.bit_len
    }

    /// Distinct values in ascending order.
    #[must_use]
    pub fn distinct_values(&self) -> &[i64] {
        &self.values
    }

    /// Storage parts (used by the format crate).
    #[must_use]
    pub fn parts(&self) -> PartsRef<'_> {
        PartsRef {
            levels: &self.levels,
            zero_counts: &self.zero_counts,
            values: &self.values,
            n: self.n,
        }
    }

    #[inline]
    fn check_range(&self, l: usize, r: usize) -> Result<(), WmError> {
        if l > r || r > self.n {
            Err(WmError::RangeOutOfBounds { l, r, n: self.n })
        } else if l == r {
            Err(WmError::EmptyRange { l, r })
        } else {
            Ok(())
        }
    }

    /// k-th smallest value in `[l, r)`; `k` is 0-based.
    ///
    /// # Errors
    /// [`WmError::RangeOutOfBounds`], [`WmError::EmptyRange`],
    /// [`WmError::KOutOfBounds`].
    pub fn quantile(&self, l: usize, r: usize, k: u64) -> Result<i64, WmError> {
        self.check_range(l, r)?;
        let span = (r - l) as u64;
        if k >= span {
            return Err(WmError::KOutOfBounds {
                k,
                l,
                r,
                len: span as usize,
            });
        }
        Ok(self.values[self.quantile_id(l, r, k) as usize])
    }

    fn quantile_id(&self, mut l: usize, mut r: usize, mut k: u64) -> u64 {
        let mut id: u64 = 0;
        for (depth, bv) in self.levels.iter().enumerate() {
            let l0 = bv.rank0(l);
            let r0 = bv.rank0(r);
            let zeros_in_range = (r0 - l0) as u64;
            if k < zeros_in_range {
                l = l0;
                r = r0;
            } else {
                let shift = (self.bit_len - 1 - depth) as u32;
                id |= 1u64 << shift;
                k -= zeros_in_range;
                let z = self.zero_counts[depth];
                l = z + l - l0;
                r = z + r - r0;
            }
        }
        id
    }

    /// Count of elements in `[l, r)` whose value satisfies
    /// `lo <= value < hi` (a half-open value range).
    ///
    /// # Errors
    /// [`WmError::RangeOutOfBounds`], [`WmError::EmptyRange`].
    pub fn range_count(&self, l: usize, r: usize, lo: i64, hi: i64) -> Result<u64, WmError> {
        self.check_range(l, r)?;
        if lo >= hi {
            return Ok(0);
        }
        // rank_lt(x): count of values < x in [l, r).
        let below_hi = self.rank_lt(l, r, hi);
        let below_lo = self.rank_lt(l, r, lo);
        Ok(below_hi - below_lo)
    }

    /// Count of elements in `[l, r)` whose value satisfies
    /// `lo <= value <= hi` (inclusive value range).
    ///
    /// This is the only way to express an upper bound of `i64::MAX`,
    /// which the half-open [`Self::range_count`] cannot represent.
    ///
    /// # Errors
    /// [`WmError::RangeOutOfBounds`], [`WmError::EmptyRange`].
    pub fn range_count_inclusive(
        &self,
        l: usize,
        r: usize,
        lo: i64,
        hi: i64,
    ) -> Result<u64, WmError> {
        self.check_range(l, r)?;
        if lo > hi {
            return Ok(0);
        }
        let at_most_hi = self.rank_le(l, r, hi);
        let below_lo = self.rank_lt(l, r, lo);
        Ok(at_most_hi - below_lo)
    }

    /// Count elements in `[l, r)` strictly smaller than `x`.
    fn rank_lt(&self, l: usize, r: usize, x: i64) -> u64 {
        self.rank_lt_traced(l, r, x).count
    }

    /// Like [`Self::rank_lt`] but records every navigation step for
    /// diagnostics; never used for correctness shortcuts.
    fn rank_lt_traced(&self, l: usize, r: usize, x: i64) -> RankLtTrace {
        let bound = self.values.partition_point(|v| *v < x) as u64;
        let span = (r - l) as u64;
        if bound == 0 {
            return RankLtTrace {
                target: x,
                bound,
                note: Some("BELOW_MIN_DISTINCT_VALUE"),
                count: 0,
                steps: Vec::new(),
            };
        }
        if self.bit_len == 0 || bound >= self.values.len() as u64 {
            return RankLtTrace {
                target: x,
                bound,
                note: Some("ABOVE_MAX_DISTINCT_VALUE_ALL_MATCH"),
                count: span,
                steps: Vec::new(),
            };
        }
        let (mut l, mut r) = (l, r);
        let mut count = 0u64;
        let mut steps = Vec::with_capacity(self.bit_len);
        for (depth, bv) in self.levels.iter().enumerate() {
            let shift = (self.bit_len - 1 - depth) as u32;
            let before = (l, r);
            let l0 = bv.rank0(l);
            let r0 = bv.rank0(r);
            let bound_bit = ((bound >> shift) & 1) as u8;
            let matched = if bound_bit == 1 {
                let zeros = (r0 - l0) as u64;
                count += zeros;
                let z = self.zero_counts[depth];
                l = z + l - l0;
                r = z + r - r0;
                zeros
            } else {
                l = l0;
                r = r0;
                0
            };
            steps.push(CountNavStep {
                depth,
                bit_position: shift,
                range_before: before,
                bound_bit,
                matched_zeros_here: matched,
                range_after: (l, r),
            });
        }
        RankLtTrace {
            target: x,
            bound,
            note: None,
            count,
            steps,
        }
    }

    /// Diagnostic variant of [`Self::quantile`]: same navigation, plus the
    /// per-level branch decisions that produced the answer.
    ///
    /// # Errors
    /// Same as [`Self::quantile`].
    pub fn explain_quantile(&self, l: usize, r: usize, k: u64) -> Result<QuantileTrace, WmError> {
        self.check_range(l, r)?;
        let span = (r - l) as u64;
        if k >= span {
            return Err(WmError::KOutOfBounds {
                k,
                l,
                r,
                len: span as usize,
            });
        }
        let (query_l, query_r) = (l, r);
        let (mut l, mut r) = (l, r);
        let mut k_rem = k;
        let mut id: u64 = 0;
        let mut steps = Vec::with_capacity(self.bit_len);
        for (depth, bv) in self.levels.iter().enumerate() {
            let before = (l, r);
            let l0 = bv.rank0(l);
            let r0 = bv.rank0(r);
            let zeros_in_range = (r0 - l0) as u64;
            let bit_position = (self.bit_len - 1 - depth) as u32;
            let k_before = k_rem;
            let chosen_bit;
            if k_rem < zeros_in_range {
                chosen_bit = 0u8;
                l = l0;
                r = r0;
            } else {
                chosen_bit = 1;
                id |= 1u64 << bit_position;
                k_rem -= zeros_in_range;
                let z = self.zero_counts[depth];
                l = z + l - l0;
                r = z + r - r0;
            }
            steps.push(QuantileStep {
                depth,
                bit_position,
                range_before: before,
                zeros_in_range,
                k_before,
                chosen_bit,
                range_after: (l, r),
            });
        }
        Ok(QuantileTrace {
            query_l,
            query_r,
            k,
            id,
            value: self.values[id as usize],
            steps,
        })
    }

    /// Count elements in `[l, r)` that are `<= x`.
    /// Handles `x == i64::MAX` without computing the overflowing `x + 1`.
    fn rank_le(&self, l: usize, r: usize, x: i64) -> u64 {
        self.rank_le_traced(l, r, x).count
    }

    /// Diagnostic variant of [`Self::range_count`]: the two rank navigations
    /// and the subtraction.
    ///
    /// # Errors
    /// Same as [`Self::range_count`].
    pub fn explain_range_count(
        &self,
        l: usize,
        r: usize,
        lo: i64,
        hi: i64,
    ) -> Result<CountTrace, WmError> {
        self.check_range(l, r)?;
        if lo >= hi {
            return Ok(CountTrace {
                lo,
                hi,
                below_lo: None,
                below_hi: RankLtTrace::empty(hi),
                count: 0,
                note: Some("EMPTY_OR_REVERSED_VALUE_RANGE"),
            });
        }
        let below_hi = self.rank_lt_traced(l, r, hi);
        let below_lo = self.rank_lt_traced(l, r, lo);
        let count = below_hi.count - below_lo.count;
        Ok(CountTrace {
            lo,
            hi,
            below_lo: Some(below_lo),
            below_hi,
            count,
            note: None,
        })
    }

    /// Diagnostic variant of [`Self::predecessor`].
    ///
    /// # Errors
    /// Same as [`Self::predecessor`].
    pub fn explain_predecessor(
        &self,
        l: usize,
        r: usize,
        x: i64,
    ) -> Result<NeighborTrace, WmError> {
        self.check_range(l, r)?;
        let lt = self.rank_lt_traced(l, r, x);
        let le = self.rank_le_traced(l, r, x);
        let present = le.count > lt.count;
        let mut selection = None;
        if lt.count > 0 {
            selection = Some(self.explain_quantile(l, r, lt.count - 1)?);
        }
        Ok(NeighborTrace {
            side: NeighborSide::Predecessor,
            x,
            count_lt: lt.count,
            count_le: le.count,
            present,
            value: selection.as_ref().map(|t| t.value),
            selection,
        })
    }

    /// Diagnostic variant of [`Self::successor`].
    ///
    /// # Errors
    /// Same as [`Self::successor`].
    pub fn explain_successor(&self, l: usize, r: usize, x: i64) -> Result<NeighborTrace, WmError> {
        self.check_range(l, r)?;
        let lt = self.rank_lt_traced(l, r, x);
        let le = self.rank_le_traced(l, r, x);
        let present = le.count > lt.count;
        let span = (r - l) as u64;
        let mut selection = None;
        if le.count < span {
            selection = Some(self.explain_quantile(l, r, le.count)?);
        }
        Ok(NeighborTrace {
            side: NeighborSide::Successor,
            x,
            count_lt: lt.count,
            count_le: le.count,
            present,
            value: selection.as_ref().map(|t| t.value),
            selection,
        })
    }

    /// Traced variant of [`Self::rank_le`].
    fn rank_le_traced(&self, l: usize, r: usize, x: i64) -> RankLtTrace {
        if x == i64::MAX {
            RankLtTrace {
                target: i64::MAX,
                bound: self.values.len() as u64,
                note: Some("MAX_INTEGER_ALL_MATCH_NO_OVERFLOW"),
                count: (r - l) as u64,
                steps: Vec::new(),
            }
        } else {
            self.rank_lt_traced(l, r, x + 1)
        }
    }

    /// Largest value in `[l, r)` strictly smaller than `x`; `None` when no
    /// such value exists. `present` reports whether a value equal to `x`
    /// exists in the range (the "equal" case is reported separately, never
    /// conflated with the predecessor).
    ///
    /// # Errors
    /// [`WmError::RangeOutOfBounds`], [`WmError::EmptyRange`].
    pub fn predecessor(&self, l: usize, r: usize, x: i64) -> Result<NeighborResult, WmError> {
        self.check_range(l, r)?;
        let count_lt = self.rank_lt(l, r, x);
        let count_le = self.rank_le(l, r, x);
        let present = count_le > count_lt;
        let value = if count_lt == 0 {
            None
        } else {
            Some(self.values[self.quantile_id(l, r, count_lt - 1) as usize])
        };
        Ok(NeighborResult { value, present })
    }

    /// Smallest value in `[l, r)` strictly greater than `x`; `None` when no
    /// such value exists. `present` reports whether a value equal to `x`
    /// exists in the range.
    ///
    /// # Errors
    /// [`WmError::RangeOutOfBounds`], [`WmError::EmptyRange`].
    pub fn successor(&self, l: usize, r: usize, x: i64) -> Result<NeighborResult, WmError> {
        self.check_range(l, r)?;
        let count_lt = self.rank_lt(l, r, x);
        let count_le = self.rank_le(l, r, x);
        let present = count_le > count_lt;
        let value = if count_le == (r - l) as u64 {
            None
        } else {
            Some(self.values[self.quantile_id(l, r, count_le) as usize])
        };
        Ok(NeighborResult { value, present })
    }
}

/// Outcome of a predecessor/successor query.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NeighborResult {
    /// The neighboring value, or `None` if it does not exist.
    pub value: Option<i64>,
    /// Whether `x` itself is present in the queried range.
    pub present: bool,
}

/// One level of a k-th-smallest navigation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QuantileStep {
    /// Level index, 0 = MSB level.
    pub depth: usize,
    /// Bit position of the compressed id inspected at this level.
    pub bit_position: u32,
    /// Active index interval before this level, half-open `[l, r)`.
    pub range_before: (usize, usize),
    /// Zero count inside the active interval at this level.
    pub zeros_in_range: u64,
    /// Remaining k at entry to this level.
    pub k_before: u64,
    /// Branch taken: 0 (zero/left) or 1 (one/right).
    pub chosen_bit: u8,
    /// Active interval after mapping through this level.
    pub range_after: (usize, usize),
}

/// Full diagnostic trace of a k-th-smallest query.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuantileTrace {
    pub query_l: usize,
    pub query_r: usize,
    pub k: u64,
    /// Compressed id reached after navigation.
    pub id: u64,
    /// Original value the id maps back to.
    pub value: i64,
    pub steps: Vec<QuantileStep>,
}

/// One level of a rank-less-than navigation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CountNavStep {
    pub depth: usize,
    pub bit_position: u32,
    pub range_before: (usize, usize),
    /// Bit of the id upper bound at this level.
    pub bound_bit: u8,
    /// Zero-subtree size added when `bound_bit == 1`.
    pub matched_zeros_here: u64,
    pub range_after: (usize, usize),
}

/// Diagnostic trace of "count values < target".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RankLtTrace {
    pub target: i64,
    /// Number of distinct values strictly below `target` (id upper bound).
    pub bound: u64,
    /// Non-null when navigation was skipped, with a machine-readable reason.
    pub note: Option<&'static str>,
    pub count: u64,
    pub steps: Vec<CountNavStep>,
}

impl RankLtTrace {
    fn empty(target: i64) -> Self {
        Self {
            target,
            bound: 0,
            note: Some("NOT_EVALUATED"),
            count: 0,
            steps: Vec::new(),
        }
    }
}

/// Diagnostic trace of a half-open value range count.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CountTrace {
    pub lo: i64,
    pub hi: i64,
    /// Navigation for `count(< lo)`; skipped when `lo >= hi`.
    pub below_lo: Option<RankLtTrace>,
    pub below_hi: RankLtTrace,
    pub count: u64,
    pub note: Option<&'static str>,
}

/// Which neighbor was queried.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NeighborSide {
    Predecessor,
    Successor,
}

/// Diagnostic trace of a predecessor/successor query.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NeighborTrace {
    pub side: NeighborSide,
    pub x: i64,
    pub count_lt: u64,
    pub count_le: u64,
    pub present: bool,
    pub value: Option<i64>,
    /// Trace of the final k-th selection, if a neighbor exists.
    pub selection: Option<QuantileTrace>,
}

/// Borrowed serialization view.
#[derive(Debug)]
pub struct PartsRef<'a> {
    pub levels: &'a [BitVector],
    pub zero_counts: &'a [usize],
    pub values: &'a [i64],
    pub n: usize,
}

/// Reason why a persisted image could not be turned into an index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InvalidParts {
    EmptyIndex,
    EmptyDistinct,
    DistinctNotSorted,
    LevelCountMismatch {
        levels: usize,
        zeros: usize,
    },
    UnexpectedBitWidth {
        levels: usize,
        expected: usize,
    },
    LevelLength {
        level: usize,
        len: usize,
        expected: usize,
    },
    ZeroCount {
        level: usize,
        stored: usize,
        expected: usize,
    },
}

impl std::fmt::Display for InvalidParts {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for InvalidParts {}

#[cfg(test)]
mod tests {
    use super::*;

    /// Independent oracle: sort a copy of the slice, then answer directly.
    fn oracle_quantile(v: &[i64], l: usize, r: usize, k: u64) -> i64 {
        let mut s: Vec<i64> = v[l..r].to_vec();
        s.sort_unstable();
        s[k as usize]
    }

    fn oracle_count(v: &[i64], l: usize, r: usize, lo: i64, hi: i64) -> u64 {
        v[l..r].iter().filter(|&&x| lo <= x && x < hi).count() as u64
    }

    fn oracle_count_inclusive(v: &[i64], l: usize, r: usize, lo: i64, hi: i64) -> u64 {
        v[l..r].iter().filter(|&&x| lo <= x && x <= hi).count() as u64
    }

    #[test]
    fn hand_computed_small_sequence() {
        // [5, 2, 5, 0, -3, 2] -> distinct [-3, 0, 2, 5] ids [3,2,3,0,0?]
        let v = [5i64, 2, 5, 0, -3, 2];
        let wm = WaveletMatrix::build(&v).unwrap();
        assert_eq!(wm.distinct_count(), 4);
        assert_eq!(wm.bit_len(), 2);

        // Whole-range order statistics (concrete expected values).
        assert_eq!(wm.quantile(0, 6, 0).unwrap(), -3);
        assert_eq!(wm.quantile(0, 6, 1).unwrap(), 0);
        assert_eq!(wm.quantile(0, 6, 2).unwrap(), 2);
        assert_eq!(wm.quantile(0, 6, 3).unwrap(), 2);
        assert_eq!(wm.quantile(0, 6, 4).unwrap(), 5);
        assert_eq!(wm.quantile(0, 6, 5).unwrap(), 5);

        // Sub-range [2, 5, 0] = [5,0,-3]: sorted [-3,0,5].
        assert_eq!(wm.quantile(2, 5, 0).unwrap(), -3);
        assert_eq!(wm.quantile(2, 5, 2).unwrap(), 5);

        // Value counting, concrete numbers.
        assert_eq!(wm.range_count(0, 6, 2, 6).unwrap(), 4); // 2,2,5,5
        assert_eq!(wm.range_count(0, 6, -3, 0).unwrap(), 1); // -3 only
        assert_eq!(wm.range_count(0, 6, 3, 5).unwrap(), 0); // [3,5): no 5
                                                            // No element equals i64::MAX, so [MIN, MAX) covers all 6 values.
        assert_eq!(wm.range_count(0, 6, -3, i64::MAX).unwrap(), 6);
        assert_eq!(wm.range_count(0, 6, i64::MIN, i64::MAX).unwrap(), 6);
        // The inclusive variant is identical here (MAX is absent).
        assert_eq!(
            wm.range_count_inclusive(0, 6, i64::MIN, i64::MAX).unwrap(),
            6
        );

        // Neighbors.
        assert_eq!(wm.predecessor(0, 6, 4).unwrap().value, Some(2));
        assert_eq!(wm.predecessor(0, 6, 2).unwrap().value, Some(0));
        assert_eq!(wm.predecessor(0, 6, -3).unwrap().value, None);
        assert!(wm.predecessor(0, 6, -3).unwrap().present);
        assert_eq!(wm.predecessor(0, 6, i64::MIN).unwrap().value, None);
        assert!(!wm.predecessor(0, 6, i64::MIN).unwrap().present);
        assert!(wm.predecessor(0, 6, 2).unwrap().present);
        assert_eq!(wm.successor(0, 6, 4).unwrap().value, Some(5));
        assert_eq!(wm.successor(0, 6, 5).unwrap().value, None);
        assert!(wm.successor(0, 6, 5).unwrap().present);
        assert_eq!(wm.successor(0, 6, -4).unwrap().value, Some(-3));
    }

    #[test]
    fn all_same_values_zero_levels() {
        let v = [42i64; 20];
        let wm = WaveletMatrix::build(&v).unwrap();
        assert_eq!(wm.bit_len(), 0);
        assert_eq!(wm.quantile(3, 17, 0).unwrap(), 42);
        assert_eq!(wm.quantile(3, 17, 13).unwrap(), 42);
        assert_eq!(wm.range_count(0, 20, 42, 43).unwrap(), 20);
        assert_eq!(wm.range_count(0, 20, 41, 42).unwrap(), 0);
        assert_eq!(wm.predecessor(0, 20, 42).unwrap().value, None);
        assert!(wm.predecessor(0, 20, 42).unwrap().present);
        assert_eq!(wm.successor(0, 20, 42).unwrap().value, None);
        assert_eq!(wm.predecessor(0, 20, 100).unwrap().value, Some(42));
    }

    #[test]
    fn signed_extremes_preserve_order() {
        let v = [i64::MAX, 0, i64::MIN, -1, 1, i64::MIN, i64::MAX];
        let wm = WaveletMatrix::build(&v).unwrap();
        assert_eq!(wm.quantile(0, 7, 0).unwrap(), i64::MIN);
        assert_eq!(wm.quantile(0, 7, 2).unwrap(), -1);
        assert_eq!(wm.quantile(0, 7, 6).unwrap(), i64::MAX);
        assert_eq!(wm.range_count(0, 7, i64::MIN, i64::MAX).unwrap(), 5);
        // Half-open excludes two occurrences of MAX; inclusive includes them.
        assert_eq!(
            wm.range_count_inclusive(0, 7, i64::MIN, i64::MAX).unwrap(),
            7
        );
        assert_eq!(
            wm.range_count_inclusive(0, 7, i64::MAX, i64::MAX).unwrap(),
            2
        );
        assert_eq!(wm.successor(0, 7, i64::MAX).unwrap().value, None);
        assert_eq!(wm.predecessor(0, 7, i64::MIN).unwrap().value, None);
        assert_eq!(wm.successor(0, 7, i64::MIN).unwrap().value, Some(-1));
        // Lo == hi and lo > hi are trivially zero.
        assert_eq!(wm.range_count(0, 7, 5, 5).unwrap(), 0);
        assert_eq!(wm.range_count(0, 7, 5, 4).unwrap(), 0);
    }

    #[test]
    fn rejection_categories_are_specific() {
        let v = [1i64, 2, 3];
        let wm = WaveletMatrix::build(&v).unwrap();
        assert_eq!(wm.quantile(0, 3, 3).unwrap_err().kind(), "K_OUT_OF_BOUNDS");
        assert_eq!(wm.quantile(1, 1, 0).unwrap_err().kind(), "EMPTY_RANGE");
        assert_eq!(
            wm.quantile(2, 4, 0).unwrap_err().kind(),
            "RANGE_OUT_OF_BOUNDS"
        );
        assert_eq!(
            wm.quantile(3, 2, 0).unwrap_err().kind(),
            "RANGE_OUT_OF_BOUNDS"
        );
        assert_eq!(
            WaveletMatrix::build(&[]).unwrap_err().kind(),
            "EMPTY_VALUES"
        );
        assert_eq!(
            wm.range_count(0, 2, 1, 2).unwrap(),
            1 // sanity: [1,2) contains exactly value 1
        );
    }

    /// Deterministic xorshift PRNG so the test needs no `rand` dependency.
    struct Rng(u64);
    impl Rng {
        fn next_u64(&mut self) -> u64 {
            let mut x = self.0;
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            self.0 = x;
            x
        }
        fn below(&mut self, m: u64) -> u64 {
            self.next_u64() % m
        }
    }

    /// Randomized differential test against independent sort/linear oracles.
    #[test]
    fn randomized_oracle_differential() {
        let mut rng = Rng(0x1234_5678_9abc_def0);
        for trial in 0..400 {
            // Mix widths: tiny universes (duplicates), 32-bit, full 64-bit.
            let n = 1 + rng.below(40) as usize;
            let mode = trial % 4;
            let v: Vec<i64> = (0..n)
                .map(|_| match mode {
                    0 => (rng.below(4) as i64) - 2,      // -2..=1, many dups
                    1 => (rng.next_u64() as u32) as i64, // nonnegative u32
                    2 => (rng.next_u64() as i32) as i64, // signed 32
                    _ => {
                        // Full i64 including extremes, biased boundaries.
                        match rng.below(20) {
                            0 => i64::MIN,
                            1 => i64::MAX,
                            2 => -1,
                            3 => 0,
                            _ => rng.next_u64() as i64,
                        }
                    }
                })
                .collect();

            let wm = WaveletMatrix::build(&v).unwrap();
            for _ in 0..20 {
                let l = rng.below(n as u64) as usize;
                let r = l + 1 + rng.below((n - l) as u64) as usize;
                let span = (r - l) as u64;
                let k = rng.below(span);

                assert_eq!(
                    wm.quantile(l, r, k).unwrap(),
                    oracle_quantile(&v, l, r, k),
                    "quantile mismatch v={v:?} [{l},{r}) k={k}"
                );

                // Counting with random value bounds, including extremes.
                let bounds: [i64; 6] = [
                    i64::MIN,
                    v[rng.below(n as u64) as usize],
                    (rng.next_u64() as i32) as i64,
                    -1,
                    0,
                    i64::MAX,
                ];
                let lo = bounds[rng.below(bounds.len() as u64) as usize];
                let hi = bounds[rng.below(bounds.len() as u64) as usize];
                let hi = if lo <= hi && lo != i64::MAX {
                    hi.max(lo.wrapping_add(1))
                } else {
                    hi
                };
                let expected = oracle_count(&v, l, r, lo, hi);
                let got = wm.range_count(l, r, lo, hi).unwrap();
                assert_eq!(
                    got, expected,
                    "count mismatch v={v:?} [{l},{r}) [{lo},{hi})"
                );

                // Inclusive upper bound is exercised separately, including
                // the case the half-open interval cannot express (hi=MAX).
                let lo2 = bounds[rng.below(bounds.len() as u64) as usize];
                let hi2 = bounds[rng.below(bounds.len() as u64) as usize].max(lo2);
                let exp_inc = oracle_count_inclusive(&v, l, r, lo2, hi2);
                let got_inc = wm.range_count_inclusive(l, r, lo2, hi2).unwrap();
                assert_eq!(
                    got_inc, exp_inc,
                    "inclusive count mismatch v={v:?} [{l},{r}) [{lo2},{hi2}]"
                );

                // Predecessor/successor vs linear scan.
                let x = bounds[rng.below(bounds.len() as u64) as usize];
                let pred = wm.predecessor(l, r, x).unwrap();
                let succ = wm.successor(l, r, x).unwrap();
                let exp_pred = v[l..r].iter().copied().filter(|&z| z < x).max();
                let exp_succ = v[l..r].iter().copied().filter(|&z| z > x).min();
                let exp_present = v[l..r].contains(&x);
                assert_eq!(pred.value, exp_pred, "pred v={v:?} [{l},{r}) x={x}");
                assert_eq!(succ.value, exp_succ, "succ v={v:?} [{l},{r}) x={x}");
                assert_eq!(pred.present, exp_present, "pred.present x={x}");
                assert_eq!(succ.present, exp_present, "succ.present x={x}");
            }
        }
    }
}
