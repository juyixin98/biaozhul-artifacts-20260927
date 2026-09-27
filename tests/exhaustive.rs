//! Exhaustive small-set checks.
//!
//! For every n from 0..=12 and several deterministic seeds, build the real
//! index and assert:
//! - every member is accepted with a slot in 0..n;
//! - the n accepted slots are exactly the permutation {0,1,...,n-1};
//! - a deterministic battery of non-members is never accepted;
//! - an empty set rejects every probe.
//!
//! The slot permutation is checked structurally against the set
//! `0..n` (not against values produced by the code under test). Large-set
//! cross-validation against the Python oracle lives in `golden.rs`.

use std::collections::HashSet;

use mphf::{build, BuildConfig, Probe, VerifyMode};

fn keys_for(n: usize, seed: u64) -> Vec<Vec<u8>> {
    // Keys that vary with seed so different seeds give genuinely different
    // hypergraphs (the builder may still retry internally).
    (0..n)
        .map(|i| format!("s{seed:02}-k{i:03}-payload-{:04}", (i * 7919 + seed as usize) % 9973).into_bytes())
        .collect()
}

fn check_one(n: usize, seed: u64, mode: VerifyMode) {
    let keys = keys_for(n, seed);
    let cfg = BuildConfig {
        base_seed: seed,
        verify: mode,
        ..Default::default()
    };
    let report = build(keys.clone(), &cfg).expect("small set must build within retry cap");
    let idx = &report.index;
    assert_eq!(idx.key_count(), n);
    assert!(idx.vertex_count() >= n);

    // Members: accepted, slots form the exact set 0..n.
    let mut slots = HashSet::new();
    for k in &keys {
        match idx.probe(k) {
            Probe::Member { slot } => {
                assert!((slot as usize) < n, "slot {slot} out of range n={n}");
                assert!(slots.insert(slot), "duplicate slot {slot} at n={n}");
            }
            other => panic!("member {:?} rejected: {other:?}", String::from_utf8_lossy(k)),
        }
    }
    assert_eq!(slots.len(), n, "missing slots at n={n}");
    assert_eq!(slots, (0..n as u64).collect::<HashSet<_>>());

    // Non-members: deterministic battery including boundary strings.
    let mut nonmembers: Vec<Vec<u8>> = vec![
        b"".to_vec(),
        b"not-present".to_vec(),
        b"sXX-k000".to_vec(),
        vec![0xff, 0x00, 0x7f],
    ];
    // Every key with one byte flipped is a distinct non-member.
    if n > 0 {
        let k0 = keys[0].clone();
        if let Some((i, _)) = k0.iter().enumerate().next() {
            let mut flipped = k0.clone();
            flipped[i] ^= 0x01;
            nonmembers.push(flipped);
        }
        // All keys from a different seed namespace.
        if n < 12 {
            for j in 0..n {
                nonmembers.push(format!("s99-k{j:03}-DIFFERENT!").into_bytes());
            }
        }
    }
    for nm in nonmembers {
        if keys.contains(&nm) {
            continue;
        }
        match idx.probe(&nm) {
            Probe::Rejected { .. } => {}
            Probe::Member { slot } => panic!(
                "non-member {:?} falsely accepted with slot {slot} (n={n}, seed={seed})",
                String::from_utf8_lossy(&nm)
            ),
            Probe::Inconclusive { reason } => {
                panic!("probe undetermined ({reason}) for n={n} seed={seed}")
            }
        }
    }
}

#[test]
fn exhaustive_small_sets_full_key_mode() {
    for n in 0..=12usize {
        for seed in [1u64, 42, 0xdead_beef, 777] {
            check_one(n, seed, VerifyMode::FullKey);
        }
    }
}

#[test]
fn exhaustive_small_sets_fingerprint_mode() {
    // Fingerprint mode has nonzero FPR, but tiny sets with explicit probe
    // battery must still behave; check 8 and 16 bit for structure.
    for n in 1..=12usize {
        for bits in [8u8, 16] {
            check_one(
                n,
                1 + bits as u64,
                VerifyMode::Fingerprint { bits },
            );
        }
    }
}

#[test]
fn empty_set_rejects_everything_in_every_mode() {
    for mode in [
        VerifyMode::FullKey,
        VerifyMode::Fingerprint { bits: 8 },
        VerifyMode::Fingerprint { bits: 16 },
    ] {
        let cfg = BuildConfig {
            verify: mode,
            ..Default::default()
        };
        let idx = build(vec![], &cfg).unwrap().index;
        assert_eq!(idx.key_count(), 0);
        for k in [b"".as_slice(), b"a", b"anything-at-all"] {
            match idx.probe(k) {
                Probe::Rejected { .. } => {}
                other => panic!("empty set accepted a key: {other:?}"),
            }
        }
    }
}
