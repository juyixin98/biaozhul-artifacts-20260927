//! Independent reference implementation: sorted-set semantics.
//!
//! Set algebra (union/intersection/difference) uses `BTreeSet` directly,
//! while rank/select/min/max are answered from a cached strictly sorted
//! vector by binary search. Nothing here shares an algorithm with
//! `hbs-core`: there are no chunks, containers, packed words, popcount or
//! bit-select routines anywhere in this file.

use std::collections::BTreeSet;

/// Reference set backed solely by the standard library.
#[derive(Debug, Clone, Default)]
pub struct ReferenceSet {
    set: BTreeSet<u32>,
    /// Lazily rebuilt sorted materialisation used for binary-search rank
    /// and indexed select. `BTreeSet::range().count()` is linear, which
    /// would make large property checks quadratic; the answers are exactly
    /// the same, just indexed.
    sorted: Vec<u32>,
}

impl ReferenceSet {
    fn from_set(set: BTreeSet<u32>) -> Self {
        let sorted = set.iter().copied().collect();
        Self { set, sorted }
    }

    pub fn new() -> Self {
        Self::default()
    }

    pub fn from_values(values: impl IntoIterator<Item = u32>) -> Self {
        Self::from_set(values.into_iter().collect())
    }

    pub fn insert(&mut self, v: u32) -> bool {
        let changed = self.set.insert(v);
        if changed {
            self.sorted = self.set.iter().copied().collect();
        }
        changed
    }

    pub fn remove(&mut self, v: u32) -> bool {
        let changed = self.set.remove(&v);
        if changed {
            self.sorted = self.set.iter().copied().collect();
        }
        changed
    }

    pub fn contains(&self, v: u32) -> bool {
        self.set.contains(&v)
    }

    pub fn len(&self) -> u64 {
        self.set.len() as u64
    }

    pub fn is_empty(&self) -> bool {
        self.set.is_empty()
    }

    pub fn iter(&self) -> std::collections::btree_set::Iter<'_, u32> {
        self.set.iter()
    }

    pub fn values_sorted(&self) -> Vec<u32> {
        self.sorted.clone()
    }

    pub fn min(&self) -> Option<u32> {
        self.sorted.first().copied()
    }

    pub fn max(&self) -> Option<u32> {
        self.sorted.last().copied()
    }

    /// Count of members strictly less than `v` (binary search).
    pub fn rank_lt(&self, v: u32) -> u64 {
        self.sorted.partition_point(|&x| x < v) as u64
    }

    /// Count of members less than or equal to `v` (binary search).
    pub fn rank_le(&self, v: u32) -> u64 {
        self.sorted.partition_point(|&x| x <= v) as u64
    }

    /// The `rank`-th member (0-based), indexed directly.
    pub fn select(&self, rank: u64) -> Option<u32> {
        self.sorted.get(rank as usize).copied()
    }

    pub fn union(&self, other: &ReferenceSet) -> ReferenceSet {
        ReferenceSet::from_set(self.set.union(&other.set).copied().collect())
    }

    pub fn intersection(&self, other: &ReferenceSet) -> ReferenceSet {
        ReferenceSet::from_set(self.set.intersection(&other.set).copied().collect())
    }

    pub fn difference(&self, other: &ReferenceSet) -> ReferenceSet {
        ReferenceSet::from_set(self.set.difference(&other.set).copied().collect())
    }

    pub fn symmetric_difference(&self, other: &ReferenceSet) -> ReferenceSet {
        ReferenceSet::from_set(self.set.symmetric_difference(&other.set).copied().collect())
    }

    pub fn is_subset(&self, other: &ReferenceSet) -> bool {
        self.set.is_subset(&other.set)
    }
}
