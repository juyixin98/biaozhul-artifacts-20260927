//! Two-dimensional Fenwick (BIT) over compressed rank space.
//!
//! Internal accumulators are [`i128`]: even when every registered point totals
//! `i64::MAX`, a rectangle containing all of them must still sum exactly
//! (rectangle sums are *not* required to fit in `i64`). Point totals
//! themselves stay `i64` and are range-checked before publication (see the
//! service layer).

use super::grid::DenseGrid;

#[derive(Debug, Clone)]
pub struct Fenwick2D {
    /// `nx + 1` rows × `ny + 1` columns, 1-based; row 0 / column 0 are padding.
    nx: usize,
    ny: usize,
    bit: Vec<i128>,
}

impl Fenwick2D {
    fn off(&self, i: usize, j: usize) -> usize {
        i * (self.ny + 1) + j
    }

    /// Build in `O(nx*ny*log nx*log ny)` from the point-total grid by replaying
    /// every cell as a point add. Coordinates are pre-registered and dense, so
    /// a freshly built index per published version is cheap, immutable and
    /// lock-free for readers.
    pub fn from_grid(grid: &DenseGrid) -> Self {
        let (nx, ny) = (grid.nx, grid.ny);
        let mut fw = Self {
            nx,
            ny,
            bit: vec![0; (nx + 1) * (ny + 1)],
        };
        for (ix, iy, v) in grid.cells() {
            if v != 0 {
                fw.point_add(ix + 1, iy + 1, v as i128);
            }
        }
        fw
    }

    fn point_add(&mut self, i: usize, j: usize, delta: i128) {
        let mut i = i;
        while i <= self.nx {
            let mut jj = j;
            while jj <= self.ny {
                let off = self.off(i, jj);
                self.bit[off] += delta;
                jj += jj.isolate_lowest_one();
            }
            i += i.isolate_lowest_one();
        }
    }

    /// Sum over ranks `i <= rx`, `j <= ry` (both 1-based cutoffs).
    fn prefix(&self, rx: usize, ry: usize) -> i128 {
        let mut sum = 0i128;
        let mut rx = rx;
        while rx > 0 {
            let mut j = ry;
            while j > 0 {
                sum += self.bit[self.off(rx, j)];
                j -= j.isolate_lowest_one();
            }
            rx -= rx.isolate_lowest_one();
        }
        sum
    }

    pub fn nx(&self) -> usize {
        self.nx
    }

    pub fn ny(&self) -> usize {
        self.ny
    }
}

/// Rectangle accessor bound to one published index. Holds the compression
/// cutoffs so query handling cannot mix tables across versions.
pub struct RectQuery<'a> {
    fw: &'a Fenwick2D,
    /// 1-based counts of registered coords ≤ each bound; `[0, nx]` / `[0, ny]`.
    rx_hi: usize,
    rx_lm: usize,
    ry_hi: usize,
    ry_lm: usize,
}

/// Human/JSON-readable breakdown of one rectangle evaluation. Exposes the
/// exact computation steps so failures can be correlated and audited.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct RectExplain {
    /// `lo > hi` on an axis (no point qualifies).
    pub empty: bool,
    /// Fenwick prefix cutoffs: number of registered coordinates ≤ bound.
    pub cutoffs: Cutoffs,
    /// Inclusion–exclusion: `p(x≤h,y≤h) − p(x≤l−1,y≤h) − p(x≤h,y≤l−1) + p(x≤l−1,y≤l−1)`.
    pub terms: Terms,
    pub sum: i128,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct Cutoffs {
    pub x_le_hi: usize,
    pub x_lt_lo: usize,
    pub y_le_hi: usize,
    pub y_lt_lo: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct Terms {
    pub hh: i128,
    pub lh: i128,
    pub hl: i128,
    pub ll: i128,
}

impl<'a> RectQuery<'a> {
    /// Build cutoffs from raw bounds. `rank_prefix(lo - 1)` is computed in
    /// i128 so `lo = i64::MIN` cannot underflow.
    pub fn new(
        fw: &'a Fenwick2D,
        table: &'a super::compress::CoordTable,
        x_lo: i128,
        x_hi: i128,
        y_lo: i128,
        y_hi: i128,
    ) -> Self {
        Self {
            fw,
            rx_hi: table.xs.rank_prefix(x_hi),
            // Strict `< lo` cutoffs avoid `lo - 1` underflow at i128::MIN;
            // for integer coordinates this is the same count.
            rx_lm: table.xs.rank_less(x_lo),
            ry_hi: table.ys.rank_prefix(y_hi),
            ry_lm: table.ys.rank_less(y_lo),
        }
    }

    /// Inclusive-rectangle sum via 2D inclusion–exclusion on prefix cutoffs.
    /// `lo > hi` (empty) naturally yields cutoffs with `rank(lo-1) > rank(hi)`
    /// on an axis; the explicit emptiness check keeps that case at exactly 0
    /// without relying on subtraction ordering.
    pub fn sum(&self) -> i128 {
        self.explain().sum
    }

    /// Same computation as [`Self::sum`], plus every cutoff and the four
    /// prefix-sum terms that justify the result.
    pub fn explain(&self) -> RectExplain {
        let empty = self.rx_hi <= self.rx_lm || self.ry_hi <= self.ry_lm;
        let hh = self.fw.prefix(self.rx_hi, self.ry_hi);
        let lh = self.fw.prefix(self.rx_lm, self.ry_hi);
        let hl = self.fw.prefix(self.rx_hi, self.ry_lm);
        let ll = self.fw.prefix(self.rx_lm, self.ry_lm);
        let sum = if empty { 0 } else { hh - lh - hl + ll };
        RectExplain {
            empty,
            cutoffs: Cutoffs {
                x_le_hi: self.rx_hi,
                x_lt_lo: self.rx_lm,
                y_le_hi: self.ry_hi,
                y_lt_lo: self.ry_lm,
            },
            terms: Terms { hh, lh, hl, ll },
            sum,
        }
    }
}
