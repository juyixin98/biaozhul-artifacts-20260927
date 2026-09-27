//! Coordinate compression kernel.
//!
//! Ordering is fixed and total: coordinates on each axis are stored **sorted
//! ascending with duplicates removed**. Ranks are 1-based (Fenwick indexing);
//! the rank-accessor functions return 0-based dense-grid indices.

use crate::model::Coord;
use std::sync::Arc;

/// Immutable, shared sorted coordinate table for one axis.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AxisTable {
    coords: Arc<Vec<Coord>>,
}

impl AxisTable {
    /// Build from an arbitrary input list: sorted ascending, deduped.
    /// Two inputs that are equal as sets always produce identical tables.
    pub fn new(mut coords: Vec<Coord>) -> Self {
        coords.sort_unstable();
        coords.dedup();
        Self {
            coords: Arc::new(coords),
        }
    }

    pub fn len(&self) -> usize {
        self.coords.len()
    }

    pub fn is_empty(&self) -> bool {
        self.coords.is_empty()
    }

    pub fn coords(&self) -> &[Coord] {
        &self.coords
    }

    /// 1-based Fenwick rank of an exact coordinate, or `None` if not registered.
    pub fn rank_of(&self, c: Coord) -> Option<usize> {
        self.coords.binary_search(&c).ok().map(|i| i + 1)
    }

    /// 0-based dense index of an exact coordinate, or `None` if not registered.
    pub fn index_of(&self, c: Coord) -> Option<usize> {
        self.coords.binary_search(&c).ok()
    }

    /// 1-based count of registered coordinates `<= v` (Fenwick prefix cutoff).
    /// `v` is `i128` so query bounds outside the `i64` range are representable.
    pub fn rank_prefix(&self, v: i128) -> usize {
        self.coords.partition_point(|c| (*c as i128) <= v)
    }

    /// 1-based count of registered coordinates strictly `< v`.
    ///
    /// For integer coordinates this equals `rank_prefix(v - 1)` but never
    /// subtracts, so `v = i128::MIN` is representable without underflow.
    pub fn rank_less(&self, v: i128) -> usize {
        self.coords.partition_point(|c| (*c as i128) < v)
    }

    pub fn contains(&self, c: Coord) -> bool {
        self.index_of(c).is_some()
    }
}

/// The two-dimensional compression: a fixed pair of sorted axis tables.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoordTable {
    pub xs: AxisTable,
    pub ys: AxisTable,
}

impl CoordTable {
    pub fn new(xs: Vec<Coord>, ys: Vec<Coord>) -> Self {
        Self {
            xs: AxisTable::new(xs),
            ys: AxisTable::new(ys),
        }
    }

    pub fn dims(&self) -> (usize, usize) {
        (self.xs.len(), self.ys.len())
    }

    /// 0-based `(ix, iy)` of a registered point, or `None` when either
    /// coordinate is absent — the caller decides rejection vs. carry-over.
    pub fn index_of(&self, x: Coord, y: Coord) -> Option<(usize, usize)> {
        match (self.xs.index_of(x), self.ys.index_of(y)) {
            (Some(ix), Some(iy)) => Some((ix, iy)),
            _ => None,
        }
    }
}
