//! Sparse, dense, interleaved and threshold distributions cross-checked
//! against the independent BTreeSet oracle.

use hbs_core::{HierBitmap, THRESHOLD};
use hbs_testkit::ReferenceSet;
use hbs_testkit::generator::{Distribution, sample};
use hbs_testkit::props::cross_check;

fn sut(values: &[u32]) -> HierBitmap {
    let mut v = values.to_vec();
    v.sort_unstable();
    v.dedup();
    HierBitmap::from_sorted_unique(&v).unwrap()
}

#[test]
fn every_distribution_agrees_with_independent_oracle() {
    for d in [
        Distribution::Sparse,
        Distribution::Dense,
        Distribution::Interleaved,
        Distribution::ContainerThreshold,
    ] {
        for seed in [1u64, 42, 777, 0xDEAD_BEEF] {
            let a = sample(d, seed);
            let b = sample(d, seed.wrapping_mul(7).wrapping_add(13));
            let report = cross_check(&a.values, &b.values);
            assert!(
                report.is_ok(),
                "distribution {:?} seed {}: {:?} (after {} checks)",
                d,
                seed,
                report.failure,
                report.checks
            );
        }
    }
}

#[test]
fn sparse_distribution_is_all_array_containers() {
    let s = sample(Distribution::Sparse, 42);
    let set = sut(&s.values);
    assert_eq!(set.stats().bitmap_containers, 0);
    assert!(set.stats().array_containers >= 1);
    // At most a handful of distinct chunks, all sparse.
    assert!(set.chunk_count() <= 200);
}

#[test]
fn dense_distribution_has_bitmap_containers() {
    let s = sample(Distribution::Dense, 1);
    let set = sut(&s.values);
    // Three 30k runs. Run i starts at i*1.5e9, whose in-chunk offsets are
    // 0, 12032, 24064; each run stays inside one chunk (offset+30000 <
    // 65536), and 30000 > 4096, so exactly three dense chunks result.
    assert_eq!(set.stats().bitmap_containers, 3, "stats: {:?}", set.stats());
    assert_eq!(set.stats().array_containers, 0, "stats: {:?}", set.stats());
    assert_eq!(set.chunk_count(), 3);
}

#[test]
fn interleaved_distribution_mixes_both_kinds() {
    let s = sample(Distribution::Interleaved, 7);
    let set = sut(&s.values);
    assert!(
        set.stats().bitmap_containers >= 10,
        "stats: {:?}",
        set.stats()
    );
    assert!(
        set.stats().array_containers >= 10,
        "stats: {:?}",
        set.stats()
    );
}

#[test]
fn threshold_sample_has_exact_container_choice() {
    let s = sample(Distribution::ContainerThreshold, 1);
    let set = sut(&s.values);
    // chunks 0,1 -> array (4095, 4096); chunk 2 -> bitmap (4097).
    for (key, kind) in hbs_testkit::fixtures::expected_threshold_container_kinds() {
        let c = set
            .chunk(&key)
            .unwrap_or_else(|| panic!("missing chunk {key}"));
        match kind {
            "array" => assert!(
                matches!(c, hbs_core::Container::Array(_)),
                "chunk {key} expected array"
            ),
            "bitmap" => assert!(
                matches!(c, hbs_core::Container::Bitmap(_)),
                "chunk {key} expected bitmap"
            ),
            _ => unreachable!(),
        }
    }
    // Exact cardinalities asserted too.
    assert_eq!(set.chunk(&0).unwrap().len(), THRESHOLD - 1);
    assert_eq!(set.chunk(&1).unwrap().len(), THRESHOLD);
    assert_eq!(set.chunk(&2).unwrap().len(), THRESHOLD + 1);
}

#[test]
fn rank_select_inverse_against_oracle_on_interleaved_data() {
    let a = sample(Distribution::Interleaved, 42);
    let set = sut(&a.values);
    let oracle = ReferenceSet::from_values(a.values.iter().copied());

    // Every rank: SUT select == oracle select, and rank_lt inverts.
    // Use the oracle's sorted materialisation directly; calling its
    // `select` (= BTreeSet::nth) for every rank would be quadratic.
    let oracle_values = oracle.values_sorted();
    for (r, &expected) in oracle_values.iter().enumerate() {
        let v = set.select(r as u64).expect("select must succeed");
        assert_eq!(v, expected, "rank {r}");
        assert_eq!(set.rank_lt(v), r as u64, "rank_lt inversion at {v}");
    }
    // Every member: rank_le(v) = rank_lt(v)+1, including boundary values.
    for v in [0, 65535, 65536, u32::MAX] {
        assert_eq!(set.rank_le(v), oracle.rank_le(v));
    }
}
