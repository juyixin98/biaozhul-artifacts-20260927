//! Suffix array construction by prefix doubling.
//!
//! Input is the coded text from [`crate::coding`] (sentinel 0, bytes 1..=256),
//! terminated by exactly one sentinel. Output `sa` satisfies
//! `coded[sa[i]..] < coded[sa[j]..]` for `i < j`.
//!
//! Algorithm: rank equivalence classes, doubled each round; each round the
//! pairs `(rank[i], rank[i + gap])` are sorted with two stable counting
//! sorts (radix sort), giving O(n log n) time with no comparisons. The unique
//! terminal sentinel makes the suffix order total and places the empty suffix
//! at `sa[0]`.

use crate::error::{Error, Result};

/// Maximum supported coded length — SA entries are stored as `u32`.
pub const MAX_TEXT_LEN: usize = u32::MAX as usize - 1;

/// Build the suffix array of a sentinel-terminated coded text.
pub fn build_sa(coded: &[u16]) -> Result<Vec<u32>> {
    let n = coded.len();
    if n == 0 {
        return Err(Error::Invariant("build_sa: empty coded text".into()));
    }
    if n > MAX_TEXT_LEN {
        return Err(Error::Invariant(format!(
            "build_sa: text length {n} exceeds u32 SA capacity"
        )));
    }
    if *coded.last().unwrap() != crate::coding::SENTINEL {
        return Err(Error::Invariant(
            "build_sa: coded text is not sentinel-terminated".into(),
        ));
    }
    if coded[..n - 1].contains(&crate::coding::SENTINEL) {
        return Err(Error::Invariant(
            "build_sa: sentinel may only appear at the last position".into(),
        ));
    }

    let n = n as u32;
    // Initial classes are the raw symbols: dense in 0..=256.
    let mut rank: Vec<u32> = coded.iter().map(|&s| s as u32).collect();
    let mut order: Vec<u32> = (0..n).collect();
    let mut tmp: Vec<u32> = vec![0; n as usize];
    // Counting-sort by first key (initial symbols, 0..256).
    counting_sort(&mut order, &mut tmp, &mut vec![0u32; 257], |i| {
        rank[i as usize]
    });

    let mut classes = 257u32;
    let mut cnt = vec![0u32; classes as usize];
    let mut gap: usize = 1;

    while gap < n as usize {
        // Stable radix sort on (rank[i], rank[i+gap]); an out-of-range second
        // key is 0. This cannot merge distinct suffixes (see module note).
        counting_sort(&mut order, &mut tmp, &mut cnt, |i| {
            let j = i as usize + gap;
            if j < n as usize { rank[j] } else { 0 }
        });
        counting_sort(&mut order, &mut tmp, &mut cnt, |i| rank[i as usize]);

        // Recompute equivalence classes from sorted pairs.
        let mut new_classes = 1u32;
        tmp[order[0] as usize] = 0;
        for w in 1..n {
            let (a, b) = (order[w as usize - 1], order[w as usize]);
            let a2 = rank.get(a as usize + gap).copied();
            let b2 = rank.get(b as usize + gap).copied();
            if rank[a as usize] != rank[b as usize] || a2 != b2 {
                new_classes += 1;
            }
            tmp[b as usize] = new_classes - 1;
        }
        std::mem::swap(&mut rank, &mut tmp);
        classes = new_classes;
        if classes == n {
            break;
        }
        if cnt.len() < classes as usize {
            cnt.resize(classes as usize, 0);
        }
        gap *= 2;
    }

    debug_assert_eq!(order[0], n - 1, "empty (sentinel) suffix must be first");
    Ok(order)
}

/// Stable counting sort of `items` in place.
///
/// `key(i)` must be in `0..cnt.len()`. On return equal keys keep their
/// previous relative order. `tmp` must be as long as `items`.
fn counting_sort(items: &mut [u32], tmp: &mut [u32], cnt: &mut [u32], key: impl Fn(u32) -> u32) {
    for c in cnt.iter_mut() {
        *c = 0;
    }
    for &i in items.iter() {
        cnt[key(i) as usize] += 1;
    }
    let mut sum = 0u32;
    for c in cnt.iter_mut() {
        let freq = *c;
        *c = sum;
        sum += freq;
    }
    for &i in items.iter() {
        let k = key(i) as usize;
        tmp[cnt[k] as usize] = i;
        cnt[k] += 1;
    }
    items.copy_from_slice(&tmp[..items.len()]);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::coding::code_with_sentinel;

    /// Independent, deliberately simple O(n^2 log n) reference SA: compare
    /// coded suffixes directly. Only used to test the production builder.
    fn reference_sa(coded: &[u16]) -> Vec<u32> {
        let mut idx: Vec<u32> = (0..coded.len() as u32).collect();
        idx.sort_by(|&a, &b| coded[a as usize..].cmp(&coded[b as usize..]));
        idx
    }

    #[test]
    fn sa_banana_like_text() {
        let coded = code_with_sentinel(b"banana");
        assert_eq!(build_sa(&coded).unwrap(), reference_sa(&coded));
        // Explicit: empty suffix first, then "a","ana","anana",...
        assert_eq!(build_sa(&coded).unwrap(), vec![6, 5, 3, 1, 0, 4, 2]);
    }

    #[test]
    fn sa_rejects_internal_sentinel_inputs() {
        assert!(matches!(build_sa(&[5, 0, 7, 0]), Err(Error::Invariant(_))));
        assert!(matches!(build_sa(&[5, 7]), Err(Error::Invariant(_))));
    }

    #[test]
    fn sa_matches_brute_force_on_repetitive_and_zero_bytes() {
        for text in [
            vec![0u8; 1],
            vec![0u8; 50],
            b"aaaaa".to_vec(),
            (0..64u16).map(|i| (i % 5) as u8).collect(),
            (0..200u32).map(|i| (i % 7) as u8).collect(),
            b"\x00\xff\x00\x00\xfe\xff".repeat(10),
        ] {
            let coded = code_with_sentinel(&text);
            assert_eq!(
                build_sa(&coded).unwrap(),
                reference_sa(&coded),
                "SA mismatch on text of len {}",
                text.len()
            );
        }
    }
}
