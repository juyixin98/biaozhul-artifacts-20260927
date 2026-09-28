//! Kernel tests: exhaustive permutation property, duplicates, empty set,
//! peeling-failure categories, bounded retries, and non-member rejection.

use mph_service::kernel::builder::{attempt_seed, build, vertex_count, BuildError};
use mph_service::kernel::graph::{build_edges, peel};
use mph_service::kernel::index::NotMemberReason;
use mph_service::kernel::Lookup;

fn k(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}

fn set(n: usize, prefix: &str) -> Vec<Vec<u8>> {
    (0..n).map(|i| k(&format!("{prefix}-{i:04}"))).collect()
}

/// Assert the mapping is a permutation of 0..n for every set 0..=40.
#[test]
fn permutation_0_to_n_minus_1_for_all_small_sizes() {
    for n in 0..=40usize {
        let keys = set(n, "perm");
        let outcome = build(&keys, 1234, 128, 1_000_000).unwrap_or_else(|e| {
            panic!("build failed for n={n}: {e}")
        });
        let idx = &outcome.index;
        assert_eq!(idx.n, n, "n mismatch at size {n}");
        assert_eq!(idx.m, vertex_count(n), "m mismatch at size {n}");

        let mut slots: Vec<u64> = keys
            .iter()
            .map(|key| match idx.lookup(key) {
                Lookup::Member { slot } => slot,
                other => panic!("expected member for size {n}: {other:?}"),
            })
            .collect();
        slots.sort_unstable();
        assert_eq!(
            slots,
            (0..n as u64).collect::<Vec<_>>(),
            "slots not a permutation of 0..n-1 at size {n}"
        );
    }
}

#[test]
fn duplicates_are_removed_before_mapping_is_built() {
    let keys = vec![
        k("a"),
        k("b"),
        k("a"),
        k("c"),
        k("b"),
        k("a"),
        k(""),
        k(""),
    ];
    let outcome = build(&keys, 7, 64, 1_000_000).unwrap();
    assert_eq!(outcome.duplicates_removed, 4, "a x2, b x1, empty x1 = 4 repeats");
    assert_eq!(outcome.index.n, 4);

    // Original keys (including repeats) still map to the same slot.
    let slot_first_a = match outcome.index.lookup(b"a") {
        Lookup::Member { slot } => slot,
        other => panic!("a should be member: {other:?}"),
    };
    let slots: Vec<u64> = [b"a".as_ref(), b"b".as_ref(), b"c".as_ref(), b"".as_ref()]
        .into_iter()
        .map(|key| match outcome.index.lookup(key) {
            Lookup::Member { slot } => slot,
            other => panic!("member expected: {other:?}"),
        })
        .collect();
    let mut sorted = slots.clone();
    sorted.sort_unstable();
    assert_eq!(sorted, vec![0, 1, 2, 3]);
    assert_eq!(slot_first_a, slots[0]);
}

#[test]
fn empty_set_builds_and_rejects_every_query() {
    let outcome = build(&[], 555, 64, 1_000_000).unwrap();
    assert_eq!(outcome.attempts, 1);
    assert_eq!(outcome.index.n, 0);
    assert_eq!(outcome.index.m, 0);
    match outcome.index.lookup(b"anything") {
        Lookup::NotMember {
            reason: NotMemberReason::EmptyIndex,
        } => {}
        other => panic!("expected EmptyIndex rejection, got {other:?}"),
    }
    match outcome.index.lookup(b"") {
        Lookup::NotMember {
            reason: NotMemberReason::EmptyIndex,
        } => {}
        other => panic!("expected EmptyIndex rejection, got {other:?}"),
    }
}

/// A synthetic 2-core independent of hashing: all 3-subsets of 4
/// vertices form a core where every vertex has degree 3, so peeling
/// removes nothing.
#[test]
fn synthetic_2_core_is_a_classified_peeling_failure() {
    let edges = vec![
        [0u32, 1, 2],
        [0, 1, 3],
        [0, 2, 3],
        [1, 2, 3],
    ];
    let err = peel(&edges, 4).expect_err("K4 3-uniform core must not peel");
    assert_eq!(err.peeled, 0);
    assert_eq!(err.total, 4);
    assert_eq!(
        err.to_string(),
        "peeling stopped with a non-empty core: peeled 0 of 4 edges"
    );
}

/// Find a concrete base seed whose *first* build attempt fails with a
/// real 2-core (not a degenerate edge) and whose second attempt
/// succeeds, using the builder's own `(n, m)` sizing.
fn find_failing_first_attempt(keys: &[Vec<u8>]) -> u64 {
    let n = keys.len();
    let m = vertex_count(n);
    for base in 0..1_000_000u64 {
        let core_fails_at_0 = match build_edges(attempt_seed(base, 0), m, keys) {
            Ok(edges) => peel(&edges, m).is_err(),
            // A degenerate edge is an ordinary retry, not the failure
            // category this helper is looking for.
            Err(_) => continue,
        };
        let succeeds_at_1 = match build_edges(attempt_seed(base, 1), m, keys) {
            Ok(edges) => peel(&edges, m).is_ok(),
            Err(_) => false,
        };
        if core_fails_at_0 && succeeds_at_1 {
            return base;
        }
    }
    panic!("no seed with a 2-core failure at attempt 0 in scan range");
}

#[test]
fn end_to_end_peeling_exhaustion_is_classified() {
    // Pick a seed where attempt 0 fails with a 2-core; max_attempts=1
    // must report the concrete PeelingExhausted category.
    let keys = vec![k("aa"), k("bb"), k("cc"), k("dd")];
    let base = find_failing_first_attempt(&keys);
    let err = build(&keys, base, 1, 1_000_000)
        .expect_err("single attempt must exhaust");
    match err {
        BuildError::PeelingExhausted { attempts, .. } => assert_eq!(attempts, 1),
        other => panic!("expected PeelingExhausted, got {other:?}"),
    }
}

#[test]
fn retry_with_seed_budget_succeeds_at_attempt_two() {
    // Concrete assertion: attempt 0 fails, attempt 1 succeeds.
    let keys = vec![k("aa"), k("bb"), k("cc"), k("dd")];
    let base = find_failing_first_attempt(&keys);
    let outcome = build(&keys, base, 8, 1_000_000).unwrap();
    assert_eq!(outcome.attempts, 2, "seed {base} must succeed at attempt 1");
    assert_eq!(outcome.seed, attempt_seed(base, 1));
    match outcome.index.lookup(b"aa") {
        Lookup::Member { slot } => assert!(slot < 4),
        other => panic!("{other:?}"),
    }
}

#[test]
fn out_of_set_keys_are_rejected_not_falsely_reported_members() {
    let members: Vec<Vec<u8>> = (b'a'..=b'z').map(|c| vec![c]).collect();
    let outcome = build(&members, 909, 64, 1_000_000).unwrap();

    // Candidate slots are returned for non-members (perfect hash works on
    // any input) but the fingerprint check must reject all of them.
    let mut rejected = 0;
    for a in 0..26u8 {
        for b in 0..26u8 {
            let key = [b'0' + a, b'0' + b];
            match outcome.index.lookup(&key) {
                Lookup::NotMember {
                    reason: NotMemberReason::FingerprintMismatch { candidate_slot },
                } => {
                    assert!(candidate_slot < 26);
                    rejected += 1;
                }
                Lookup::Member { slot } => {
                    panic!("false-positive membership at slot {slot} for {key:?}")
                }
                other => panic!("unexpected {other:?}"),
            }
        }
    }
    assert_eq!(rejected, 26 * 26);

    // Mutations of member keys must be rejected too.
    for member in &members {
        for &flip in &[0x01, 0x80, 0xFF] {
            let mut bad = member.clone();
            bad[0] ^= flip;
            if bad == *member || members.contains(&bad) {
                continue;
            }
            assert!(matches!(
                outcome.index.lookup(&bad),
                Lookup::NotMember {
                    reason: NotMemberReason::FingerprintMismatch { .. }
                }
            ));
        }
    }
}

#[test]
fn over_limit_is_a_distinct_error_category() {
    let err = build(&set(5, "x"), 1, 64, 4).expect_err("limit must trigger");
    assert!(matches!(
        err,
        BuildError::TooManyKeys { got: 5, limit: 4 }
    ));
}

/// Real build at scale (asymptotic regime, n > 2048): the mapping must
/// still be an exact 0..n-1 permutation and foreign keys must reject.
#[test]
fn large_set_is_an_exact_permutation_and_rejects_foreign_keys() {
    let n = 5_000usize;
    let keys: Vec<Vec<u8>> = (0..n).map(|i| k(&format!("rec:{i:08}"))).collect();
    let outcome = build(&keys, 0xC0FFEE, 128, 1_000_000).unwrap();
    assert!(outcome.attempts >= 1 && outcome.attempts <= 128);
    assert_eq!(outcome.index.m, (n as f64 * 1.23).ceil() as usize);

    let mut seen = vec![false; n];
    for key in &keys {
        match outcome.index.lookup(key) {
            Lookup::Member { slot } => {
                assert!(!seen[slot as usize], "slot {slot} assigned twice");
                seen[slot as usize] = true;
            }
            other => panic!("member rejected: {other:?}"),
        }
    }
    assert!(seen.into_iter().all(|v| v), "not a permutation");

    // Deterministic foreign-key probes must all be fingerprint-rejected.
    for i in 0..500u32 {
        let foreign = format!("foreign:{i:08}");
        assert!(matches!(
            outcome.index.lookup(foreign.as_bytes()),
            Lookup::NotMember {
                reason: NotMemberReason::FingerprintMismatch { .. }
            }
        ));
    }
}
