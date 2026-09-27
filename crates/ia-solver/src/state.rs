//! Abstract states: scalar interval environment plus the array abstraction.
//!
//! Arrays use a **weak per-cell map plus a summary**:
//! * `cells[i]` is the interval of the concrete element at static index `i`
//!   when that index is known exactly;
//! * `summary` accumulates every value written through an index that is not a
//!   single point, and is joined into reads whose index cannot be resolved to
//!   one cell.
//!
//! Reads at a point index inside bounds therefore stay precise even after
//! non-constant writes elsewhere, which is what the array-index tests rely on.
use ia_intervals::Interval;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArrayDomain {
    pub cells: Vec<Interval>,
    pub summary: Interval,
}

impl ArrayDomain {
    pub fn zeroed(len: usize) -> Self {
        Self {
            cells: vec![Interval::point(0); len],
            summary: Interval::BOTTOM,
        }
    }

    pub fn read(&self, index: Interval) -> Interval {
        match index {
            Interval::Bottom => Interval::Bottom,
            Interval::Range { lo, hi } => {
                let mut acc = self.summary;
                // Only in-range cells contribute well-defined reads.
                let start = lo.max(0) as usize;
                let end = (hi.min(self.cells.len() as i64 - 1)).max(-1);
                if end >= 0 {
                    for cell in &self.cells[start..=end as usize] {
                        acc = acc.join(*cell);
                    }
                }
                acc
            }
        }
    }

    /// Strong update when `index` is exactly one in-range cell, otherwise a
    /// weak update over the covered cells plus the summary. Returns the
    /// in-range portion actually updated (the caller checks OOB separately).
    pub fn write(&mut self, index: Interval, value: Interval) {
        let Interval::Range { lo, hi } = index else {
            return;
        };
        let n = self.cells.len() as i64;
        let start = lo.max(0);
        let end = hi.min(n - 1);
        if start == end && start >= 0 && start < n {
            // Strong update: the single concrete target is known.
            self.cells[start as usize] = value;
            return;
        }
        if start <= end {
            for cell in &mut self.cells[start as usize..=end as usize] {
                *cell = cell.join(value);
            }
        }
        self.summary = self.summary.join(value);
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AbsState {
    pub(crate) bottom: bool,
    pub vars: BTreeMap<String, Interval>,
    pub arrays: BTreeMap<String, ArrayDomain>,
}

impl AbsState {
    pub(crate) fn fresh() -> Self {
        Self {
            bottom: false,
            vars: BTreeMap::new(),
            arrays: BTreeMap::new(),
        }
    }

    pub fn bottom() -> Self {
        Self {
            bottom: true,
            vars: BTreeMap::new(),
            arrays: BTreeMap::new(),
        }
    }

    pub fn is_bottom(&self) -> bool {
        self.bottom
    }

    pub fn var(&self, name: &str) -> Interval {
        if self.bottom {
            return Interval::BOTTOM;
        }
        self.vars.get(name).copied().unwrap_or(Interval::BOTTOM)
    }

    pub fn set_var(&mut self, name: impl Into<String>, iv: Interval) {
        if self.bottom {
            return;
        }
        self.vars.insert(name.into(), iv);
    }

    /// Least upper bound.
    pub fn join(&self, other: &AbsState) -> AbsState {
        if self.bottom {
            return other.clone();
        }
        if other.bottom {
            return self.clone();
        }
        let mut vars = self.vars.clone();
        for (k, v) in &other.vars {
            let e = vars.entry(k.clone()).or_insert(Interval::BOTTOM);
            *e = e.join(*v);
        }
        let mut arrays = self.arrays.clone();
        for (name, other_arr) in &other.arrays {
            let arr = arrays.entry(name.clone()).or_insert_with(|| ArrayDomain {
                cells: vec![Interval::BOTTOM; other_arr.cells.len()],
                summary: Interval::BOTTOM,
            });
            arr.summary = arr.summary.join(other_arr.summary);
            for (a, b) in arr.cells.iter_mut().zip(other_arr.cells.iter()) {
                *a = a.join(*b);
            }
        }
        AbsState {
            bottom: false,
            vars,
            arrays,
        }
    }

    /// Pointwise jump-to-bound widening (see [`Interval::widen`]).
    pub fn widen(&self, newer: &AbsState) -> AbsState {
        // `self` is the previous invariant, `newer` the candidate; widening
        // jumps on every endpoint that grew in `newer`.
        if self.bottom {
            return newer.clone();
        }
        if newer.bottom {
            return self.clone();
        }
        let mut vars = self.vars.clone();
        for (k, v) in &newer.vars {
            let prev = self.vars.get(k).copied().unwrap_or(Interval::BOTTOM);
            let e = vars.entry(k.clone()).or_insert(Interval::BOTTOM);
            *e = v.widen(prev);
        }
        let mut arrays = self.arrays.clone();
        for (name, newer_arr) in &newer.arrays {
            let arr = arrays.entry(name.clone()).or_insert_with(|| ArrayDomain {
                cells: vec![Interval::BOTTOM; newer_arr.cells.len()],
                summary: Interval::BOTTOM,
            });
            arr.summary = newer_arr.summary.widen(arr.summary);
            for (cell, new_cell) in arr.cells.iter_mut().zip(newer_arr.cells.iter()) {
                *cell = new_cell.widen(*cell);
            }
        }
        AbsState {
            bottom: false,
            vars,
            arrays,
        }
    }

    /// Bounded narrowing: pointwise meet with the candidate.
    pub fn narrow(&self, candidate: &AbsState) -> AbsState {
        if self.bottom || candidate.bottom {
            return AbsState::bottom();
        }
        let mut vars = BTreeMap::new();
        for (k, v) in &self.vars {
            let c = candidate.vars.get(k).copied().unwrap_or(Interval::TOP);
            vars.insert(k.clone(), v.meet(c));
        }
        let mut arrays = self.arrays.clone();
        for (name, arr) in arrays.iter_mut() {
            if let Some(cand) = candidate.arrays.get(name) {
                arr.summary = arr.summary.meet(cand.summary);
                for (cell, c) in arr.cells.iter_mut().zip(cand.cells.iter()) {
                    *cell = cell.meet(*c);
                }
            }
        }
        AbsState {
            bottom: false,
            vars,
            arrays,
        }
    }

    pub fn subset_of(&self, other: &AbsState) -> bool {
        if other.bottom {
            return self.bottom;
        }
        if self.bottom {
            return true;
        }
        for (k, v) in &self.vars {
            let o = other.vars.get(k).copied().unwrap_or(Interval::BOTTOM);
            if !v.subset_of(o) {
                return false;
            }
        }
        for (name, arr) in &self.arrays {
            let Some(o) = other.arrays.get(name) else {
                return false;
            };
            if !arr.summary.subset_of(o.summary) {
                return false;
            }
            for (a, b) in arr.cells.iter().zip(o.cells.iter()) {
                if !a.subset_of(*b) {
                    return false;
                }
            }
        }
        true
    }
}
