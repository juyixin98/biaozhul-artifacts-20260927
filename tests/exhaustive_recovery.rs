//! End-to-end exhaustive recovery tests through the real service + filesystem.
//!
//! For each small profile, for *every* subset of 1..=m shards removed from
//! disk, the service must reconstruct the payload byte-for-byte identical to
//! what was stored. The expected bytes are the original input, not anything
//! produced by the kernel.

mod common;

use common::*;
use ec_service::config::BUILTIN_PROFILES;
use ec_service::erasure;

#[tokio::test]
async fn exhaustive_all_erasure_patterns_via_service() {
    let payloads: Vec<Vec<u8>> = vec![
        vec![],
        vec![0x00],
        (0..7u8).collect(),
        b"the quick brown fox jumps over".to_vec(),
        vec![0xff; 13],
        vec![0xa5; 33], // crosses shard boundaries for k=4
    ];

    let mut patterns = 0usize;
    for &(k, m) in BUILTIN_PROFILES {
        for (pi, payload) in payloads.iter().enumerate() {
            let dir = unique_data_dir(&format!("exh-{k}-{m}-{pi}"));
            let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
            let oid = format!("obj-{k}-{m}-{pi}");
            state.put_object(&oid, payload, k, m).await.unwrap();

            let total = (k + m) as usize;
            // Every subset of size 1..=m of shard indices to remove.
            for n_gone in 1..=m as usize {
                for combo in combinations(total, n_gone) {
                    for &idx in &combo {
                        delete_shard_file(&dir, &oid, idx as u8);
                    }
                    let report = state.inspect(&oid).await.unwrap();
                    assert_eq!(
                        report.status, "degraded_recoverable",
                        "(k={k},m={m}) combo={combo:?}"
                    );
                    assert_eq!(
                        report.missing_shards,
                        combo.iter().map(|i| *i as u8).collect::<Vec<_>>()
                    );
                    assert!(report.corrupt_shards.is_empty());
                    assert!(report.recoverable);
                    assert_eq!(report.margin, (m - n_gone as u8) as i64);

                    // The central guarantee: exact original bytes, no fabrication.
                    let got = state.get_object(&oid).await.unwrap();
                    assert_eq!(got, *payload, "(k={k},m={m}) combo={combo:?}");

                    // Repair restores the full set on disk.
                    let repair = state.repair(&oid).await.unwrap();
                    assert!(repair.post_repair_verified);
                    assert_eq!(repair.status_after, "intact");
                    let post = state.inspect(&oid).await.unwrap();
                    assert_eq!(post.status, "intact");
                    assert_eq!(post.ok_shards.len(), total);
                    patterns += 1;
                }
            }
            let _ = std::fs::remove_dir_all(&dir);
        }
    }
    // k1m1:2, k2m1:3, k3m2: C(5,1)+C(5,2)=15, k4m2: C(6,1)+C(6,2)=21
    // per payload: 41 combos across profiles; times 6 payloads = 246.
    assert_eq!(patterns, (2 + 3 + 15 + 21) * payloads.len());
}

#[tokio::test]
async fn repaired_shards_are_bitwise_equal_to_originals() {
    // Repaired parity/data shards must equal the originally encoded bytes,
    // not merely satisfy the equations (parity shards have a fixed
    // construction, so this catches systematic-form drift).
    let dir = unique_data_dir("repaired-bits");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let data = b"deterministic parity reconstruction";
    let (k, m) = (3u8, 2u8);
    let manifest = state.put_object("o", data, k, m).await.unwrap();
    let original: Vec<Vec<u8>> = (0..(k + m))
        .map(|i| read_shard_file(&dir, "o", i).unwrap())
        .collect();

    // Destroy a parity shard AND a data shard.
    delete_shard_file(&dir, "o", 1);
    delete_shard_file(&dir, "o", 4);
    state.repair("o").await.unwrap();
    for i in 0..(k + m) {
        assert_eq!(
            read_shard_file(&dir, "o", i).unwrap(),
            original[i as usize],
            "rebuilt shard {i} differs from original"
        );
    }
    let _ = manifest.manifest_digest;
    let _ = std::fs::remove_dir_all(&dir);
}

/// All combinations of `n` indices from `0..total`.
fn combinations(total: usize, n: usize) -> Vec<Vec<usize>> {
    fn rec(start: usize, n: usize, total: usize, cur: &mut Vec<usize>, out: &mut Vec<Vec<usize>>) {
        if n == 0 {
            out.push(cur.clone());
            return;
        }
        for i in start..=total - n {
            cur.push(i);
            rec(i + 1, n - 1, total, cur, out);
            cur.pop();
        }
    }
    let mut out = Vec::new();
    rec(0, n, total, &mut Vec::new(), &mut out);
    out
}

#[tokio::test]
async fn kernel_direct_exhaustive_combinations_are_singular_free() {
    // Property check over larger profiles that don't ship as allowed
    // service profiles: any k rows of the systematic matrix are invertible
    // (guarantees MDS-style recoverability for every erasure pattern).
    for &(k, m) in &[(5u8, 3u8), (10u8, 5u8)] {
        let em = erasure::encoding_matrix(k, m).unwrap();
        let total = (k + m) as usize;
        for combo in combinations(total, k as usize) {
            let a: Vec<Vec<u8>> = combo.iter().map(|&i| em[i].clone()).collect();
            assert!(
                erasure::gf_invertible(&a),
                "rows {combo:?} of (k={k},m={m}) must be invertible"
            );
        }
    }
}
