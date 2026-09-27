//! Dense `nx × ny` grid of `i64` point totals in compressed index space.

use crate::model::Weight;

#[derive(Debug, Clone)]
pub struct DenseGrid {
    pub nx: usize,
    pub ny: usize,
    /// Row-major, `cell(ix, iy) = data[ix * ny + iy]`.
    data: Vec<Weight>,
}

impl DenseGrid {
    pub fn zeros(nx: usize, ny: usize) -> Self {
        Self {
            nx,
            ny,
            data: vec![0; nx.checked_mul(ny).expect("grid dimensions overflow usize")],
        }
    }

    #[inline]
    fn offset(&self, ix: usize, iy: usize) -> usize {
        debug_assert!(ix < self.nx && iy < self.ny);
        ix * self.ny + iy
    }

    pub fn get(&self, ix: usize, iy: usize) -> Weight {
        self.data[self.offset(ix, iy)]
    }

    pub fn add(&mut self, ix: usize, iy: usize, delta: Weight) -> Option<()> {
        let off = self.offset(ix, iy);
        self.data[off] = self.data[off].checked_add(delta)?;
        Some(())
    }

    pub fn set(&mut self, ix: usize, iy: usize, v: Weight) {
        let off = self.offset(ix, iy);
        self.data[off] = v;
    }

    pub fn cells(&self) -> impl Iterator<Item = (usize, usize, Weight)> + '_ {
        self.data
            .iter()
            .enumerate()
            .map(move |(off, &v)| (off / self.ny, off % self.ny, v))
    }

    /// Total of every cell; used in tests/diagnostics.
    pub fn total(&self) -> i128 {
        self.data.iter().map(|&v| v as i128).sum()
    }
}
