//! The top-level hierarchical set and whole-universe operations.

use std::collections::BTreeMap;

use crate::container::Container;
use crate::iter::HierIter;

/// High 16 bits identifying a chunk.
pub type ChunkKey = u16;

/// Break a full value into (chunk key, low bits).
#[inline]
pub fn split(v: u32) -> (ChunkKey, u16) {
    ((v >> 16) as u16, (v & 0xFFFF) as u16)
}

/// Combine chunk key and low bits.
#[inline]
pub fn join(key: ChunkKey, low: u16) -> u32 {
    ((key as u32) << 16) | low as u32
}

/// Structural statistics, useful for tests that assert container choice and
/// for the observability endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SetStats {
    /// Number of non-empty chunks stored.
    pub chunks: usize,
    /// Chunks held sparsely (array container).
    pub array_containers: usize,
    /// Chunks held densely (bitmap container).
    pub bitmap_containers: usize,
}

/// A hierarchical bitmap set over the full `u32` universe.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct HierBitmap {
    chunks: BTreeMap<ChunkKey, Container>,
}

impl HierBitmap {
    /// Empty set.
    pub fn new() -> Self {
        Self {
            chunks: BTreeMap::new(),
        }
    }

    /// Build from a strictly increasing slice of `u32`.
    ///
    /// Groups consecutive values by chunk in a single linear pass, then
    /// builds each chunk's container in one go, so construction is O(n)
    /// rather than n individual sorted insertions.
    pub fn from_sorted_unique(values: &[u32]) -> Result<Self, crate::CoreError> {
        if values.windows(2).any(|w| w[0] >= w[1]) {
            return Err(crate::CoreError::NotSortedUnique);
        }
        let mut set = Self::new();
        let mut i = 0;
        while i < values.len() {
            let key = split(values[i]).0;
            let start = i;
            while i < values.len() && split(values[i]).0 == key {
                i += 1;
            }
            let lows: Vec<u16> = values[start..i].iter().map(|&v| split(v).1).collect();
            let container = Container::from_sorted_unique(&lows)?;
            set.chunks.insert(key, container);
        }
        Ok(set)
    }

    pub(crate) fn chunks_iter(&self) -> std::collections::btree_map::Iter<'_, ChunkKey, Container> {
        self.chunks.iter()
    }

    /// Iterate `(chunk key, container)` in ascending chunk order.
    pub fn chunks(&self) -> impl Iterator<Item = (ChunkKey, &Container)> {
        self.chunks.iter().map(|(k, c)| (*k, c))
    }

    /// Mutable chunk accessor for the format/store layers.
    pub fn chunk_mut(&mut self, key: ChunkKey) -> Option<&mut Container> {
        self.chunks.get_mut(&key)
    }

    /// Read-only chunk accessor.
    pub fn chunk(&self, key: &ChunkKey) -> Option<&Container> {
        self.chunks.get(key)
    }

    /// Insert a trusted (already decoded) container for a chunk.
    pub fn insert_chunk(&mut self, key: ChunkKey, container: Container) {
        if container.is_empty() {
            self.chunks.remove(&key);
        } else {
            self.chunks.insert(key, container);
        }
    }

    /// Insert a value; true if it was not already present.
    pub fn insert(&mut self, v: u32) -> bool {
        use std::collections::btree_map::Entry;
        let (key, low) = split(v);
        let empty = || {
            Container::Array(
                crate::Array::from_trusted(Vec::new(), 0).expect("empty input is sorted unique"),
            )
        };
        match self.chunks.entry(key) {
            Entry::Vacant(e) => {
                let (c, changed) = empty().insert(low);
                e.insert(c);
                changed
            }
            Entry::Occupied(mut e) => {
                // Move the real container out (no clone), let insert possibly
                // switch representation, then put the result back.
                let (c, changed) = std::mem::replace(e.get_mut(), empty()).insert(low);
                *e.get_mut() = c;
                changed
            }
        }
    }

    /// Remove a value; true if it was present. Chunks that become empty drop.
    pub fn remove(&mut self, v: u32) -> bool {
        let (key, low) = split(v);
        let mut changed = false;
        if let Some(c) = self.chunks.remove(&key) {
            let (c, did) = c.remove(low);
            changed = did;
            if !c.is_empty() {
                self.chunks.insert(key, c);
            }
        }
        changed
    }

    /// Membership.
    pub fn contains(&self, v: u32) -> bool {
        let (key, low) = split(v);
        self.chunks.get(&key).is_some_and(|c| c.contains(low))
    }

    /// Cardinality. Sum of per-chunk cardinalities — never an expansion.
    ///
    /// The sum is `u64`: the universe holds 2^32 values, and `u64` keeps
    /// totals over many sets from overflowing as well.
    pub fn len(&self) -> u64 {
        self.chunks.values().map(|c| c.len() as u64).sum()
    }

    pub fn is_empty(&self) -> bool {
        self.chunks.is_empty()
    }

    /// Number of distinct chunks in use.
    pub fn chunk_count(&self) -> usize {
        self.chunks.len()
    }

    /// Smallest value, if any.
    pub fn min(&self) -> Option<u32> {
        let (&k, c) = self.chunks.first_key_value()?;
        c.select(0).map(|low| join(k, low))
    }

    /// Largest value, if any.
    pub fn max(&self) -> Option<u32> {
        let (&k, c) = self.chunks.last_key_value()?;
        c.select(c.len() - 1).map(|low| join(k, low))
    }

    /// Rank with exclusive upper bound: count of values `< v`.
    ///
    /// `v == u32::MAX + 1` is expressed as `rank_lt(u32::MAX) + contains(MAX)`
    /// or simply [`Self::len`]; callers that need an inclusive endpoint use
    /// [`Self::rank_le`].
    pub fn rank_lt(&self, v: u32) -> u64 {
        let (key, low) = split(v);
        let mut total = 0u64;
        // Entire chunks before `key`.
        for (_, c) in self.chunks.range(..key) {
            total += c.len() as u64;
        }
        // Within the target chunk: positions 0..low.
        if let Some(c) = self.chunks.get(&key) {
            total += c.rank_lt(low as usize) as u64;
        }
        total
    }

    /// Rank with inclusive upper bound: count of values `<= v`.
    pub fn rank_le(&self, v: u32) -> u64 {
        let (key, low) = split(v);
        let mut total = 0u64;
        for (_, c) in self.chunks.range(..key) {
            total += c.len() as u64;
        }
        if let Some(c) = self.chunks.get(&key) {
            total += c.rank_le(low) as u64;
        }
        total
    }

    /// Value of the `rank`-th member (0-based). Returns `None` when
    /// `rank >= len`; `u64` input prevents callers from failing at
    /// `u32::MAX + 1`.
    pub fn select(&self, rank: u64) -> Option<u32> {
        if rank >= self.len() {
            return None;
        }
        let mut remaining = rank;
        for (&key, c) in &self.chunks {
            let len = c.len() as u64;
            if remaining < len {
                return c.select(remaining as usize).map(|low| join(key, low));
            }
            remaining -= len;
        }
        None
    }

    /// Lazy ascending iterator over full `u32` values.
    pub fn iter(&self) -> HierIter<'_> {
        HierIter::new(self)
    }

    /// Structural statistics.
    pub fn stats(&self) -> SetStats {
        let mut s = SetStats::default();
        for c in self.chunks.values() {
            s.chunks += 1;
            match c {
                Container::Array(_) => s.array_containers += 1,
                Container::Bitmap(_) => s.bitmap_containers += 1,
            }
        }
        s
    }

    // -- whole-set algebra: chunk-wise, no whole-set expansion -------------

    /// Union.
    pub fn union(&self, other: &HierBitmap) -> HierBitmap {
        let mut out = self.clone();
        for (&key, cb) in &other.chunks {
            out.chunks
                .entry(key)
                .and_modify(|c| *c = c.union(cb))
                .or_insert_with(|| cb.clone());
        }
        out
    }

    /// Intersection.
    pub fn intersection(&self, other: &HierBitmap) -> HierBitmap {
        let mut out = BTreeMap::new();
        for (&key, ca) in &self.chunks {
            if let Some(cb) = other.chunks.get(&key) {
                let c = ca.intersection(cb);
                if !c.is_empty() {
                    out.insert(key, c);
                }
            }
        }
        HierBitmap { chunks: out }
    }

    /// Set difference `self - other`.
    pub fn difference(&self, other: &HierBitmap) -> HierBitmap {
        let mut out = BTreeMap::new();
        for (&key, ca) in &self.chunks {
            let c = match other.chunks.get(&key) {
                Some(cb) => ca.difference(cb),
                None => ca.clone(),
            };
            if !c.is_empty() {
                out.insert(key, c);
            }
        }
        HierBitmap { chunks: out }
    }

    /// Symmetric difference.
    pub fn symmetric_difference(&self, other: &HierBitmap) -> HierBitmap {
        let mut out = self.clone();
        for (&key, cb) in &other.chunks {
            out.chunks
                .entry(key)
                .and_modify(|c| {
                    let d = c.symmetric_difference(cb);
                    *c = d;
                })
                .or_insert_with(|| cb.clone());
            if let Some(c) = out.chunks.get(&key)
                && c.is_empty()
            {
                out.chunks.remove(&key);
            }
        }
        out
    }

    /// Every member of `self` is in `other`.
    pub fn is_subset(&self, other: &HierBitmap) -> bool {
        self.chunks
            .iter()
            .all(|(key, c)| match other.chunks.get(key) {
                None => false,
                Some(o) => c.is_subset(o),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set_of(vals: &[u32]) -> HierBitmap {
        let mut s = HierBitmap::new();
        for &v in vals {
            s.insert(v);
        }
        s
    }

    #[test]
    fn insert_remove_contains_and_chunks() {
        let mut s = HierBitmap::new();
        assert!(s.insert(0));
        assert!(!s.insert(0));
        assert!(s.insert(u32::MAX));
        assert!(s.insert(0x0000_FFFF));
        assert!(s.insert(0x0001_0000));
        assert_eq!(s.chunk_count(), 3);
        assert_eq!(s.len(), 4);
        assert!(s.contains(u32::MAX));
        assert!(s.remove(u32::MAX));
        assert!(!s.contains(u32::MAX));
        assert_eq!(s.len(), 3);
        assert_eq!(s.min(), Some(0));
        assert_eq!(s.max(), Some(0x0001_0000));
    }

    #[test]
    fn rank_select_inverse_and_monotone() {
        let vals = [0u32, 1, 2, 65535, 65536, 131071, 131072, u32::MAX];
        let s = set_of(&vals);
        // select(k) = the k-th member; rank_le(select(k)) = k+1.
        for (k, &v) in vals.iter().enumerate() {
            assert_eq!(s.select(k as u64), Some(v));
            assert_eq!(s.rank_le(v), k as u64 + 1);
            assert_eq!(s.rank_lt(v), k as u64);
        }
        assert_eq!(s.select(vals.len() as u64), None);
        assert_eq!(s.rank_lt(u32::MAX), (vals.len() - 1) as u64);
        assert_eq!(s.rank_le(u32::MAX), vals.len() as u64);
        // monotonicity over probe points
        let mut prev = 0u64;
        for v in (0..140000u32).step_by(7919) {
            let r = s.rank_le(v);
            assert!(r >= prev);
            prev = r;
        }
    }

    #[test]
    fn set_algebra_cross_check() {
        // Sparse + dense interleaving across many chunks.
        let mut v1 = Vec::new();
        let mut v2 = Vec::new();
        for i in 0..200_000u32 {
            if i % 3 != 0 {
                v1.push(i);
            }
            if i % 3 != 1 {
                v2.push(i);
            }
        }
        v2.push(u32::MAX);
        let a = set_of(&v1);
        let b = set_of(&v2);
        let sa: std::collections::BTreeSet<u32> = v1.iter().copied().collect();
        let sb: std::collections::BTreeSet<u32> = v2.iter().copied().collect();

        assert!(a.stats().bitmap_containers > 0);
        assert!(a.stats().array_containers > 0);

        assert_eq!(
            a.union(&b).iter().collect::<Vec<_>>(),
            sa.union(&sb).copied().collect::<Vec<_>>()
        );
        assert_eq!(
            a.intersection(&b).iter().collect::<Vec<_>>(),
            sa.intersection(&sb).copied().collect::<Vec<_>>()
        );
        assert_eq!(
            a.difference(&b).iter().collect::<Vec<_>>(),
            sa.difference(&sb).copied().collect::<Vec<_>>()
        );
        assert_eq!(
            a.symmetric_difference(&b).iter().collect::<Vec<_>>(),
            sa.symmetric_difference(&sb).copied().collect::<Vec<_>>()
        );
        assert_eq!(a.union(&b).len(), sa.union(&sb).count() as u64);
        assert_eq!(
            a.intersection(&b).len(),
            sa.intersection(&sb).count() as u64
        );
        assert!(a.intersection(&b).is_subset(&a));
        assert!(a.difference(&b).is_subset(&a));
    }

    #[test]
    fn threshold_at_4096_keeps_ordered_unique() {
        // 4096 values in one chunk -> array; 4097 -> bitmap; same membership.
        let v: Vec<u32> = (0..4097u32).map(|x| x * 2).collect();
        let mut s = HierBitmap::new();
        for (i, x) in v.iter().enumerate() {
            s.insert(*x);
            let stats = s.stats();
            if i < 4096 {
                assert_eq!(stats.bitmap_containers, 0);
            } else {
                assert_eq!(stats.bitmap_containers, 1);
            }
        }
        assert!(s.chunk(&0).unwrap().contains(8192));
        assert!(!s.chunk(&0).unwrap().contains(1));
    }

    #[test]
    fn empty_and_full_boundaries() {
        let mut s = HierBitmap::new();
        assert_eq!(s.rank_le(0), 0);
        assert_eq!(s.select(0), None);
        assert_eq!(s.min(), None);
        assert_eq!(s.max(), None);
        s.insert(u32::MAX);
        assert_eq!(s.rank_le(u32::MAX), 1);
        assert_eq!(s.select(0), Some(u32::MAX));
        s.remove(u32::MAX);
        assert!(s.is_empty());
    }
}
