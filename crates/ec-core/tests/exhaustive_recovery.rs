//! Enumerate EVERY recoverable erasure combination for the small configs and
//! compare recovered bytes byte-for-byte with the golden original.
//!
//! For configs (k=3,m=2) and (k=4,m=2), shard indices are 0..n. We enumerate:
//!   - every erased subset of size 0..=m (all recoverable), and
//!   - independently every surviving subset of exactly k shards (each must
//!     alone recover the original).
//! Expected bytes come from the Python oracle fixtures, never from this
//! crate's encoder. Beyond-tolerance erasures (m+1 missing) must fail with the
//! specific INSUFFICIENT_SHARDS category.

mod common;

use ec_core::error::EcError;
use ec_core::reed_solomon::{reconstruct_data, truncate_to_original, Shard};
use ec_core::CodecConfig;

fn combinations(n: usize, r: usize) -> Vec<Vec<usize>> {
    if r == 0 {
        return vec![vec![]];
    }
    if r > n {
        return vec![];
    }
    let mut out = Vec::new();
    let mut cur: Vec<usize> = Vec::new();
    fn rec(start: usize, n: usize, r: usize, cur: &mut Vec<usize>, out: &mut Vec<Vec<usize>>) {
        if cur.len() == r {
            out.push(cur.clone());
            return;
        }
        for i in start..n {
            cur.push(i);
            rec(i + 1, n, r, cur, out);
            cur.pop();
        }
    }
    rec(0, n, r, &mut cur, &mut out);
    out
}

#[test]
fn every_recoverable_erasure_pattern_recovers_byte_for_byte() {
    let mut total_patterns = 0usize;
    for g in common::golden_cases() {
        let (k, m, n) = (g.k, g.m, g.k + g.m);
        let cfg = CodecConfig::new(k as u16, m as u16).unwrap();

        for erased_count in 0..=m {
            for erased in combinations(n, erased_count) {
                let erased_set: std::collections::BTreeSet<usize> =
                    erased.iter().copied().collect();
                let available: Vec<Shard> = (0..n)
                    .filter(|i| !erased_set.contains(i))
                    .map(|i| Shard::new(i as u16, g.shards[i].clone()))
                    .collect();

                let recon = reconstruct_data(&cfg, available, g.shard_len)
                    .unwrap_or_else(|e| {
                        panic!("case {} erased {erased:?}: unexpected error {e}", g.case_id)
                    });
                let got = truncate_to_original(
                    &cfg,
                    &recon.data_shards,
                    g.shard_len,
                    g.original.len() as u64,
                )
                .unwrap();
                assert_eq!(
                    got, g.original,
                    "case {} erased combination {erased:?}: recovered bytes differ from golden original",
                    g.case_id
                );
                total_patterns += 1;
            }
        }
    }
    // Counts: k3m2 -> sum C(5,r) r=0..2 = 1+5+10 = 16 (two such cases);
    //        k4m2 -> 1+6+15 = 22. Total 16*2 + 22 = 54.
    assert_eq!(total_patterns, 54, "exhaustive pattern count drift");
}

#[test]
fn every_exactly_k_survivor_subset_recovers_byte_for_byte() {
    let mut total = 0usize;
    for g in common::golden_cases() {
        let (k, m, n) = (g.k, g.m, g.k + g.m);
        let cfg = CodecConfig::new(k as u16, m as u16).unwrap();
        for survivors in combinations(n, k) {
            let available: Vec<Shard> = survivors
                .iter()
                .map(|&i| Shard::new(i as u16, g.shards[i].clone()))
                .collect();
            let recon = reconstruct_data(&cfg, available, g.shard_len).unwrap();
            let got = truncate_to_original(
                &cfg,
                &recon.data_shards,
                g.shard_len,
                g.original.len() as u64,
            )
            .unwrap();
            assert_eq!(got, g.original, "case {} survivors {survivors:?}", g.case_id);
            total += 1;
        }
    }
    // C(5,3)=10 per k3m2 case (x2) + C(6,4)=15 = 35.
    assert_eq!(total, 35);
}

#[test]
fn exceeding_fault_tolerance_fails_with_insufficient_shards_and_no_data() {
    // m+1 erasures => only k-1 shards => MUST refuse with the exact category.
    for g in common::golden_cases() {
        let (k, m, n) = (g.k, g.m, g.k + g.m);
        let cfg = CodecConfig::new(k as u16, m as u16).unwrap();
        let too_many_erased = m + 1;
        for erased in combinations(n, too_many_erased) {
            let erased_set: std::collections::BTreeSet<usize> =
                erased.iter().copied().collect();
            let available: Vec<Shard> = (0..n)
                .filter(|i| !erased_set.contains(i))
                .map(|i| Shard::new(i as u16, g.shards[i].clone()))
                .collect();
            assert_eq!(available.len(), k - 1);
            let err = reconstruct_data(&cfg, available, g.shard_len).unwrap_err();
            assert_eq!(
                err,
                EcError::InsufficientShards {
                    available: k - 1,
                    required: k,
                },
                "case {} erased {erased:?}",
                g.case_id
            );
            assert_eq!(err.code(), "INSUFFICIENT_SHARDS");
        }
    }
}

#[test]
fn rebuild_every_missing_shard_matches_golden_bytes() {
    // Targeted repair: with any m erasures, rebuild the erased shards and
    // compare rebuilt shard bytes with the golden encoder output.
    for g in common::golden_cases() {
        let (k, m, n) = (g.k, g.m, g.k + g.m);
        let cfg = CodecConfig::new(k as u16, m as u16).unwrap();
        for erased in combinations(n, m) {
            let erased_set: std::collections::BTreeSet<usize> =
                erased.iter().copied().collect();
            let available: Vec<Shard> = (0..n)
                .filter(|i| !erased_set.contains(i))
                .map(|i| Shard::new(i as u16, g.shards[i].clone()))
                .collect();
            let targets: Vec<usize> = erased.clone();
            let (_, rebuilt) =
                ec_core::reed_solomon::rebuild_shards(&cfg, available, g.shard_len, &targets)
                    .unwrap();
            assert_eq!(rebuilt.len(), erased.len());
            for shard in rebuilt {
                assert_eq!(
                    shard.data, g.shards[shard.index as usize],
                    "case {} targets {erased:?}, rebuilt shard {} differs from golden",
                    g.case_id, shard.index
                );
            }
        }
    }
}

#[test]
fn duplicate_shard_index_is_rejected_with_exact_category() {
    let g = common::case_by_id("k3m2_l31");
    let cfg = CodecConfig::new(3, 2).unwrap();
    let dup = vec![
        Shard::new(0, g.shards[0].clone()),
        Shard::new(0, g.shards[0].clone()),
        Shard::new(1, g.shards[1].clone()),
    ];
    let err = reconstruct_data(&cfg, dup, g.shard_len).unwrap_err();
    assert_eq!(err, EcError::DuplicateShardIndex(0));
    assert_eq!(err.code(), "DUPLICATE_SHARD_INDEX");
}

#[test]
fn single_corrupted_shard_is_detected_by_digest_and_recovery_still_succeeds() {
    // One shard flips a byte; its position-aware digest must fail, and the
    // remaining k shards recover the original.
    use ec_core::verify::{digest_shard, verify_shard};
    let g = common::case_by_id("k3m2_l31");
    let cfg = CodecConfig::new(3, 2).unwrap();

    for victim in 0..g.shards.len() {
        let mut corrupted = g.shards[victim].clone();
        corrupted[0] ^= 0x01;
        // The digest MUST classify the shard as bad.
        let expected = digest_shard(victim as u16, &g.shards[victim]);
        assert!(
            !verify_shard(victim as u16, &corrupted, &expected),
            "flipped byte in shard {victim} unexpectedly verified"
        );

        // Treat the bad shard as an erasure and recover from any k others.
        let chosen: Vec<usize> = (0..g.shards.len())
            .filter(|&i| i != victim)
            .take(g.k)
            .collect();
        let available: Vec<Shard> = chosen
            .iter()
            .map(|&i| Shard::new(i as u16, g.shards[i].clone()))
            .collect();
        let recon = reconstruct_data(&cfg, available, g.shard_len).unwrap();
        let got = truncate_to_original(
            &cfg,
            &recon.data_shards,
            g.shard_len,
            g.original.len() as u64,
        )
        .unwrap();
        assert_eq!(got, g.original, "victim {victim}");
    }
}
