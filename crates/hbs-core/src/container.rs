//! The two physical containers and per-chunk set algebra.

use crate::error::CoreError;
use crate::iter::ContainerValues;
use crate::select::{rank_le_word, rank_lt_word, select64};

/// Fixed switching threshold.
///
/// A chunk with `THRESHOLD` values or fewer is stored sparsely as an
/// [`Array`]; with more, densely as a [`Bitmap`]. The representation at the
/// exact boundary is therefore deterministic.
pub const THRESHOLD: usize = 4096;

/// Words per dense bitmap (65,536 bits / 64).
pub const BITMAP_WORDS: usize = 1024;

// ---------------------------------------------------------------------------
// Sparse container
// ---------------------------------------------------------------------------

/// Sparse container: sorted, unique 16-bit values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Array {
    values: Vec<u16>,
}

impl Array {
    /// Construct from a slice that must be strictly increasing.
    ///
    /// More than [`THRESHOLD`] values is accepted here; use
    /// [`Container::from_sorted_unique`] to pick the representation.
    pub fn from_sorted_unique(values: &[u16]) -> Result<Self, CoreError> {
        if values.windows(2).any(|w| w[0] >= w[1]) {
            return Err(CoreError::NotSortedUnique);
        }
        Ok(Self {
            values: values.to_vec(),
        })
    }

    /// Trusted constructor used by the format decoder and by algebra code.
    ///
    /// Invariants are still verified; the claimed cardinality is checked
    /// against the data. Deserialization rejects the empty container, since an
    /// empty chunk must never be serialised.
    pub fn from_trusted(values: Vec<u16>, cardinality: usize) -> Result<Self, CoreError> {
        if values.windows(2).any(|w| w[0] >= w[1]) {
            return Err(CoreError::NotSortedUnique);
        }
        if cardinality != values.len() {
            return Err(CoreError::CardinalityMismatch {
                claimed: cardinality,
                actual: values.len(),
            });
        }
        Ok(Self { values })
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.values.len()
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    #[inline]
    pub fn values(&self) -> &[u16] {
        &self.values
    }

    #[inline]
    pub fn contains(&self, v: u16) -> bool {
        self.values.binary_search(&v).is_ok()
    }

    /// Insert a value; returns the resulting representation.
    ///
    /// The array converts itself to a dense [`Bitmap`] the first insertion
    /// that raises cardinality to `THRESHOLD + 1`.
    pub fn insert(self, v: u16) -> (Container, bool) {
        match self.values.binary_search(&v) {
            Ok(_) => (Container::Array(self), false),
            Err(pos) => {
                if self.values.len() < THRESHOLD {
                    let mut a = self;
                    a.values.insert(pos, v);
                    (Container::Array(a), true)
                } else {
                    let mut bmp = Bitmap::from_values(&self.values);
                    bmp.set(v);
                    (Container::Bitmap(bmp), true)
                }
            }
        }
    }

    /// Remove a value (array stays an array; it can only shrink).
    pub fn remove(mut self, v: u16) -> (Array, bool) {
        match self.values.binary_search(&v) {
            Ok(pos) => {
                self.values.remove(pos);
                (self, true)
            }
            Err(_) => (self, false),
        }
    }
}

// ---------------------------------------------------------------------------
// Dense container
// ---------------------------------------------------------------------------

/// Dense container: 1,024 packed 64-bit words covering every 16-bit value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bitmap {
    words: [u64; BITMAP_WORDS],
    cardinality: usize,
}

impl Default for Bitmap {
    fn default() -> Self {
        Self::new()
    }
}

impl Bitmap {
    /// All-zero bitmap.
    pub fn new() -> Self {
        Self {
            words: [0; BITMAP_WORDS],
            cardinality: 0,
        }
    }

    /// Build from any sequence of 16-bit values, setting each bit.
    pub fn from_values(values: &[u16]) -> Self {
        let mut b = Self::new();
        for &v in values {
            b.set(v);
        }
        b
    }

    /// Trusted constructor used by the format decoder.
    ///
    /// Verifies that the claimed cardinality matches the packed words.
    pub fn from_trusted(words: [u64; BITMAP_WORDS], cardinality: usize) -> Result<Self, CoreError> {
        let actual = words.iter().map(|w| w.count_ones() as usize).sum();
        if actual != cardinality {
            return Err(CoreError::CardinalityMismatch {
                claimed: cardinality,
                actual,
            });
        }
        Ok(Self { words, cardinality })
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.cardinality
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.cardinality == 0
    }

    #[inline]
    pub fn words(&self) -> &[u64; BITMAP_WORDS] {
        &self.words
    }

    #[inline]
    pub fn contains(&self, v: u16) -> bool {
        self.words[(v >> 6) as usize] & (1u64 << (v & 63)) != 0
    }

    #[inline]
    fn set(&mut self, v: u16) -> bool {
        let w = (v >> 6) as usize;
        let bit = 1u64 << (v & 63);
        if self.words[w] & bit == 0 {
            self.words[w] |= bit;
            self.cardinality += 1;
            true
        } else {
            false
        }
    }

    #[inline]
    fn clear(&mut self, v: u16) -> bool {
        let w = (v >> 6) as usize;
        let bit = 1u64 << (v & 63);
        if self.words[w] & bit != 0 {
            self.words[w] &= !bit;
            self.cardinality -= 1;
            true
        } else {
            false
        }
    }

    /// Insert while dense. A bitmap never shrinks on insert.
    pub fn insert(mut self, v: u16) -> (Bitmap, bool) {
        let changed = self.set(v);
        (self, changed)
    }

    /// Remove; the result is canonicalised (a bitmap at or below the
    /// threshold becomes an [`Array`]).
    pub fn remove(mut self, v: u16) -> (Container, bool) {
        let changed = self.clear(v);
        (canonicalise_bitmap(self.words, self.cardinality), changed)
    }

    /// Number of set bits at positions `0..=i`.
    pub fn rank_le(&self, i: u16) -> usize {
        let word = (i >> 6) as usize;
        let bit = (i & 63) as usize;
        let mut count = 0usize;
        for w in &self.words[..word] {
            count += w.count_ones() as usize;
        }
        count + rank_le_word(self.words[word], bit)
    }

    /// Number of set bits at positions `0..i`.
    pub fn rank_lt(&self, i: usize) -> usize {
        debug_assert!(i <= 65536);
        let word = i >> 6;
        let bit = i & 63;
        let mut count = 0usize;
        for w in &self.words[..word] {
            count += w.count_ones() as usize;
        }
        if bit != 0 {
            count + rank_lt_word(self.words[word], bit)
        } else {
            count
        }
    }

    /// Position of the `rank`-th set bit (0-based).
    pub fn select(&self, rank: usize) -> Option<u16> {
        if rank >= self.cardinality {
            return None;
        }
        let mut remaining = rank;
        for (idx, &w) in self.words.iter().enumerate() {
            let pc = w.count_ones() as usize;
            if remaining < pc {
                let bit = select64(w, remaining)?;
                return Some((idx * 64 + bit) as u16);
            }
            remaining -= pc;
        }
        None
    }
}

// ---------------------------------------------------------------------------
// Canonical container
// ---------------------------------------------------------------------------

/// Canonical per-chunk representation: sparse or dense.
///
/// The dense variant is deliberately much larger (8,192 bytes of packed
/// words) and stored inline rather than boxed: every bit operation would
/// otherwise pay an extra pointer chase. Most chunks in a sparse set are
/// the small variant; dense chunks are where the indirection would hurt
/// most, so the size asymmetry is a conscious trade-off.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Container {
    /// Sparse sorted array (cardinality `0..=THRESHOLD`).
    Array(Array),
    /// Dense bitmap (cardinality `THRESHOLD+1..=65536`).
    Bitmap(Bitmap),
}

/// Pick the canonical representation for packed words.
///
/// At most [`THRESHOLD`] set bits become an array; more stay a bitmap.
pub fn canonicalise_bitmap(words: [u64; BITMAP_WORDS], cardinality: usize) -> Container {
    if cardinality <= THRESHOLD {
        let mut values = Vec::with_capacity(cardinality);
        for (wi, &w) in words.iter().enumerate() {
            let mut x = w;
            while x != 0 {
                let bit = x.trailing_zeros() as usize;
                values.push((wi * 64 + bit) as u16);
                x &= x - 1;
            }
        }
        Container::Array(Array { values })
    } else {
        Container::Bitmap(Bitmap { words, cardinality })
    }
}

/// Pick the canonical representation for a sorted, unique vector.
pub fn canonicalise_vec(mut values: Vec<u16>) -> Container {
    // Callers always pass sorted-unique data; defensively normalising here
    // keeps the threshold decision correct even if one ever does not.
    values.sort_unstable();
    values.dedup();
    if values.len() <= THRESHOLD {
        Container::Array(Array { values })
    } else {
        Container::Bitmap(Bitmap::from_values(&values))
    }
}

impl Container {
    /// Build from a strictly increasing slice, choosing sparse vs dense by
    /// the fixed threshold.
    pub fn from_sorted_unique(values: &[u16]) -> Result<Self, CoreError> {
        if values.windows(2).any(|w| w[0] >= w[1]) {
            return Err(CoreError::NotSortedUnique);
        }
        Ok(canonicalise_vec(values.to_vec()))
    }

    /// Build from an arbitrary (possibly unsorted / duplicated) slice.
    pub fn from_unsorted(values: &[u16]) -> Self {
        let mut v = values.to_vec();
        v.sort_unstable();
        v.dedup();
        canonicalise_vec(v)
    }

    #[inline]
    pub fn len(&self) -> usize {
        match self {
            Container::Array(a) => a.len(),
            Container::Bitmap(b) => b.len(),
        }
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    #[inline]
    pub fn contains(&self, v: u16) -> bool {
        match self {
            Container::Array(a) => a.contains(v),
            Container::Bitmap(b) => b.contains(v),
        }
    }

    /// Insert, returning the (possibly switched) container plus a flag
    /// indicating whether the value was newly added.
    pub fn insert(self, v: u16) -> (Container, bool) {
        match self {
            Container::Array(a) => a.insert(v),
            Container::Bitmap(b) => {
                let (b, changed) = b.insert(v);
                (Container::Bitmap(b), changed)
            }
        }
    }

    /// Remove; bitmaps shrink back to an array at the threshold.
    pub fn remove(self, v: u16) -> (Container, bool) {
        match self {
            Container::Array(a) => {
                let (a, changed) = a.remove(v);
                (Container::Array(a), changed)
            }
            Container::Bitmap(b) => b.remove(v),
        }
    }

    /// Values `0..=i` in this chunk.
    pub fn rank_le(&self, i: u16) -> usize {
        match self {
            Container::Array(a) => match a.values().binary_search(&i) {
                Ok(pos) => pos + 1,
                Err(pos) => pos,
            },
            Container::Bitmap(b) => b.rank_le(i),
        }
    }

    /// Values in positions `0..i` (i up to 65,536).
    pub fn rank_lt(&self, i: usize) -> usize {
        debug_assert!(i <= 65536);
        match self {
            Container::Array(a) => a.values().partition_point(|&v| (v as usize) < i),
            Container::Bitmap(b) => b.rank_lt(i),
        }
    }

    /// Position of the `rank`-th value (0-based).
    pub fn select(&self, rank: usize) -> Option<u16> {
        match self {
            Container::Array(a) => a.values().get(rank).copied(),
            Container::Bitmap(b) => b.select(rank),
        }
    }

    /// Lazily iterate the chunk's values; never allocates the whole set.
    pub fn iter(&self) -> ContainerValues<'_> {
        ContainerValues::new(self)
    }

    // -- set algebra ------------------------------------------------------

    /// Union.
    pub fn union(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => merge_union(a.values(), b.values()),
            (Container::Bitmap(x), Container::Bitmap(y)) => bitwise(x, y, |w1, w2| w1 | w2),
            // Mixed: collect into the bitmap side.
            (Container::Array(a), Container::Bitmap(b))
            | (Container::Bitmap(b), Container::Array(a)) => {
                let mut words = *b.words();
                for &v in a.values() {
                    words[(v >> 6) as usize] |= 1u64 << (v & 63);
                }
                let card = words.iter().map(|w| w.count_ones() as usize).sum();
                canonicalise_bitmap(words, card)
            }
        }
    }

    /// Intersection.
    pub fn intersection(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => {
                merge_intersection(a.values(), b.values())
            }
            (Container::Bitmap(x), Container::Bitmap(y)) => bitwise(x, y, |w1, w2| w1 & w2),
            (Container::Array(a), Container::Bitmap(b))
            | (Container::Bitmap(b), Container::Array(a)) => {
                let values: Vec<u16> = a
                    .values()
                    .iter()
                    .copied()
                    .filter(|&v| b.contains(v))
                    .collect();
                canonicalise_vec(values)
            }
        }
    }

    /// Set difference `self - other`.
    pub fn difference(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => merge_difference(a.values(), b.values()),
            (Container::Bitmap(x), Container::Bitmap(y)) => bitwise(x, y, |w1, w2| w1 & !w2),
            (Container::Array(a), Container::Bitmap(b)) => {
                let values: Vec<u16> = a
                    .values()
                    .iter()
                    .copied()
                    .filter(|&v| !b.contains(v))
                    .collect();
                canonicalise_vec(values)
            }
            (Container::Bitmap(b), Container::Array(a)) => {
                let mut words = *b.words();
                for &v in a.values() {
                    words[(v >> 6) as usize] &= !(1u64 << (v & 63));
                }
                let card = words.iter().map(|w| w.count_ones() as usize).sum();
                canonicalise_bitmap(words, card)
            }
        }
    }

    /// Symmetric difference.
    pub fn symmetric_difference(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => merge_symdiff(a.values(), b.values()),
            (Container::Bitmap(x), Container::Bitmap(y)) => bitwise(x, y, |w1, w2| w1 ^ w2),
            (Container::Array(a), Container::Bitmap(b))
            | (Container::Bitmap(b), Container::Array(a)) => {
                let mut words = *b.words();
                for &v in a.values() {
                    words[(v >> 6) as usize] ^= 1u64 << (v & 63);
                }
                let card = words.iter().map(|w| w.count_ones() as usize).sum();
                canonicalise_bitmap(words, card)
            }
        }
    }

    /// Subset test without materialising either side.
    pub fn is_subset(&self, other: &Container) -> bool {
        match (self, other) {
            (Container::Array(a), _) => a.values().iter().all(|&v| other.contains(v)),
            (Container::Bitmap(b), Container::Array(a)) => {
                b.len() <= a.len() && a.values().iter().all(|&v| b.contains(v))
            }
            (Container::Bitmap(x), Container::Bitmap(y)) => x
                .words()
                .iter()
                .zip(y.words())
                .all(|(w1, w2)| w1 & !w2 == 0),
        }
    }
}

// -- sorted-merge helpers ----------------------------------------------------

fn merge_union(a: &[u16], b: &[u16]) -> Container {
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(b[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    canonicalise_vec(out)
}

fn merge_symdiff(a: &[u16], b: &[u16]) -> Container {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(b[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    canonicalise_vec(out)
}

fn merge_intersection(a: &[u16], b: &[u16]) -> Container {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    canonicalise_vec(out)
}

fn merge_difference(a: &[u16], b: &[u16]) -> Container {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    canonicalise_vec(out)
}

/// Apply a bitwise op to two packed bitmaps, recomputing cardinality and
/// canonicalising the result (an intersection may collapse to an array).
fn bitwise(x: &Bitmap, y: &Bitmap, op: impl Fn(u64, u64) -> u64) -> Container {
    let mut words = [0u64; BITMAP_WORDS];
    let mut card = 0usize;
    for ((out, wx), wy) in words.iter_mut().zip(x.words()).zip(y.words()) {
        *out = op(*wx, *wy);
        card += out.count_ones() as usize;
    }
    canonicalise_bitmap(words, card)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn evens(lo: u16, hi: u16) -> Vec<u16> {
        (lo..=hi).step_by(2).collect()
    }

    #[test]
    fn threshold_switch_up_on_insert() {
        // Exactly THRESHOLD values -> array; one more dense.
        let c = Container::from_sorted_unique(&evens(0, 8190)).unwrap();
        assert_eq!(c.len(), THRESHOLD);
        assert!(matches!(c, Container::Array(_)));
        // 8191 is odd and absent -> raises cardinality above threshold.
        let (c, changed) = c.insert(8191);
        assert!(changed);
        assert_eq!(c.len(), THRESHOLD + 1);
        assert!(matches!(c, Container::Bitmap(_)));
        // duplicate insert reports false and keeps dense
        let (c, changed) = c.insert(8191);
        assert!(!changed);
        assert!(matches!(c, Container::Bitmap(_)));
    }

    #[test]
    fn threshold_switch_down_on_remove() {
        let dense: Vec<u16> = (0..=(THRESHOLD as u16)).collect();
        let mut c = Container::from_sorted_unique(&dense).unwrap();
        assert!(matches!(c, Container::Bitmap(_)));
        c = c.remove(0).0;
        assert_eq!(c.len(), THRESHOLD);
        assert!(matches!(c, Container::Array(_)), "must shrink at boundary");
    }

    #[test]
    fn container_rank_select_array_and_bitmap() {
        let vals = evens(0, 8190);
        for mk in [
            Container::from_sorted_unique(&vals).unwrap(),
            Container::from_unsorted(&vals),
        ] {
            assert_eq!(mk.rank_le(100), 51);
            assert_eq!(mk.rank_lt(100), 50);
            assert_eq!(mk.select(0), Some(0));
            assert_eq!(mk.select(50), Some(100));
            assert_eq!(mk.select(4096), None);
            assert_eq!(mk.rank_le(65535), 4096);
        }
    }

    #[test]
    fn dense_all_ones_boundaries() {
        let all: Vec<u16> = (0..=65535u16).collect();
        let c = Container::from_sorted_unique(&all).unwrap();
        let b = match c {
            Container::Bitmap(b) => b,
            _ => panic!("full chunk must be dense"),
        };
        assert_eq!(b.rank_le(0), 1);
        assert_eq!(b.rank_le(65535), 65536);
        assert_eq!(b.rank_lt(65536), 65536);
        assert_eq!(b.select(0), Some(0));
        assert_eq!(b.select(65535), Some(65535));
        assert_eq!(b.select(65536), None);
    }

    fn to_vec(c: &Container) -> Vec<u16> {
        c.iter().collect()
    }

    #[test]
    fn algebra_matches_sorted_merge() {
        let a: Vec<u16> = (0..100u16).filter(|x| x % 2 == 0).collect();
        let b: Vec<u16> = (50..150u16).filter(|x| x % 2 == 1).collect();
        let ca = Container::from_unsorted(&a);
        let cb = Container::from_unsorted(&b);

        // Dense-dense path too.
        let da = {
            let mut v = evens(0, 10000);
            v.extend(1..100u16);
            Container::from_unsorted(&v)
        };
        let db = {
            let mut v = evens(2, 10002);
            v.extend(90..200u16);
            Container::from_unsorted(&v)
        };
        let variants = [(&ca, &cb), (&da, &db), (&ca, &db), (&db, &ca)];
        for (x, y) in variants {
            let xs: std::collections::BTreeSet<u16> = x.iter().collect();
            let ys: std::collections::BTreeSet<u16> = y.iter().collect();

            let expect_union: Vec<u16> = xs.union(&ys).copied().collect();
            let expect_inter: Vec<u16> = xs.intersection(&ys).copied().collect();
            let expect_diff: Vec<u16> = xs.difference(&ys).copied().collect();
            let expect_sym: Vec<u16> = xs.symmetric_difference(&ys).copied().collect();

            assert_eq!(to_vec(&x.union(y)), expect_union);
            assert_eq!(to_vec(&x.intersection(y)), expect_inter);
            assert_eq!(to_vec(&x.difference(y)), expect_diff);
            assert_eq!(to_vec(&x.symmetric_difference(y)), expect_sym);
            assert!(x.intersection(y).is_subset(&x.union(y)));
            assert!(x.difference(y).is_subset(x));
        }
    }

    #[test]
    fn unsorted_rejected_or_normalised() {
        assert_eq!(
            Array::from_sorted_unique(&[1, 1]),
            Err(CoreError::NotSortedUnique)
        );
        assert_eq!(
            Array::from_sorted_unique(&[2, 1]),
            Err(CoreError::NotSortedUnique)
        );
        let c = Container::from_unsorted(&[3, 1, 2, 2]);
        assert_eq!(to_vec(&c), vec![1, 2, 3]);
    }

    #[test]
    fn bitmap_iter_order() {
        let c = Container::from_unsorted(&[0, 1, 63, 64, 100, 65535]);
        assert_eq!(to_vec(&c), vec![0, 1, 63, 64, 100, 65535]);
    }
}
