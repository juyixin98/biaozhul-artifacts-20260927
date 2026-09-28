//! Index kernel: coordinate-compressed integer sequence + wavelet matrix.
//!
//! ## Query conventions (load-bearing, shared by the API and docs)
//!
//! * Intervals are **half-open** `[l, r)` with `0 <= l < r <= len`.
//!   `l == r` is rejected as [`WmError::EmptyRange`]; any other malformed
//!   window is [`WmError::InvalidRange`].
//! * `k` is **0-based**: `k = 0` asks for the minimum, `k = r - l - 1` for
//!   the maximum. `k >= r - l` is rejected as [`WmError::KOutOfBounds`].
//! * Queries are served by the wavelet matrix's index traversal — never by
//!   sorting a slice of the window.

use crate::compress::Compressor;
use crate::error::WmError;
use crate::wavelet_matrix::{Level, WaveletMatrix};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WmIndex {
    comp: Compressor,
    wm: WaveletMatrix,
    len: usize,
}

impl WmIndex {
    /// Build the index over `data`. Empty input is rejected.
    pub fn build(data: &[i64]) -> Result<Self, WmError> {
        if data.is_empty() {
            return Err(WmError::EmptyInput);
        }
        let comp = Compressor::new(data);
        let distinct = comp.distinct();
        // Bits needed to represent the largest rank `distinct - 1`.
        // A single distinct value still needs height 1.
        let height = ((distinct as u64).next_power_of_two().trailing_zeros()).max(1) as usize;
        let ranks = comp.compress(data);
        let wm = WaveletMatrix::build(&ranks, height);
        Ok(WmIndex {
            comp,
            wm,
            len: data.len(),
        })
    }

    /// Rebuild from persisted parts; validates consistency.
    pub fn restore(
        values: Vec<i64>,
        height: usize,
        levels: Vec<Level>,
        len: usize,
    ) -> Result<Self, WmError> {
        let comp = Compressor::from_sorted(values).map_err(WmError::CorruptFormat)?;
        if len == 0 {
            return Err(WmError::CorruptFormat("zero sequence length".into()));
        }
        // height must be wide enough to address every distinct rank.
        if (1u128 << height) < comp.distinct() as u128 {
            return Err(WmError::CorruptFormat(
                "height too small for the value table".into(),
            ));
        }
        let wm = WaveletMatrix::restore(height, len, levels).map_err(WmError::CorruptFormat)?;
        Ok(WmIndex { comp, wm, len })
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        false
    }

    pub fn distinct(&self) -> usize {
        self.comp.distinct()
    }

    pub fn height(&self) -> usize {
        self.wm.height()
    }

    pub(crate) fn levels(&self) -> &[Level] {
        self.wm.levels()
    }

    pub(crate) fn value_table(&self) -> &[i64] {
        self.comp.values()
    }

    fn check_window(&self, l: usize, r: usize) -> Result<usize, WmError> {
        if l > r || r > self.len {
            return Err(WmError::InvalidRange {
                l,
                r,
                len: self.len,
            });
        }
        if l == r {
            return Err(WmError::EmptyRange { l, r });
        }
        Ok(r - l)
    }

    /// 0-based k-th smallest value in `[l, r)`.
    pub fn kth_smallest(&self, l: usize, r: usize, k: usize) -> Result<i64, WmError> {
        let window = self.check_window(l, r)?;
        if k >= window {
            return Err(WmError::KOutOfBounds { k, window });
        }
        let rank = self.wm.quantile(l, r, k) as usize;
        self.comp.value_at(rank).ok_or_else(|| {
            WmError::CorruptFormat("quantile produced rank outside value table".into())
        })
    }

    /// Count of values `v` in `[l, r)` with `v < bound` (upper-exclusive).
    pub fn count_lt(&self, l: usize, r: usize, bound: i64) -> Result<usize, WmError> {
        self.check_window(l, r)?;
        let upper_rank = self.comp.count_distinct_below(bound) as u64;
        Ok(self.wm.range_freq(l, r, upper_rank))
    }

    /// Count of values `v` in `[l, r)` with `lo <= v < hi`.
    pub fn count_range(&self, l: usize, r: usize, lo: i64, hi: i64) -> Result<usize, WmError> {
        if lo >= hi {
            // Validate the window anyway so malformed windows still fail.
            self.check_window(l, r)?;
            return Ok(0);
        }
        let below_hi = self.count_lt(l, r, hi)?;
        let below_lo = self.count_lt(l, r, lo)?;
        Ok(below_hi - below_lo)
    }

    /// Largest value in `[l, r)` strictly below `bound`, if any.
    pub fn predecessor(&self, l: usize, r: usize, bound: i64) -> Result<Option<i64>, WmError> {
        self.check_window(l, r)?;
        let below = self.count_lt(l, r, bound)?;
        if below == 0 {
            return Ok(None);
        }
        // The largest value below `bound` is the (below-1)-th smallest.
        self.kth_smallest(l, r, below - 1).map(Some)
    }

    /// Smallest value in `[l, r)` greater than or equal to `bound`, if any.
    pub fn successor(&self, l: usize, r: usize, bound: i64) -> Result<Option<i64>, WmError> {
        let window = self.check_window(l, r)?;
        let below = self.count_lt(l, r, bound)?;
        if below == window {
            return Ok(None);
        }
        self.kth_smallest(l, r, below).map(Some)
    }
}
