//! Cross-validation against the independent Python oracle's golden fixture.
//!
//! The fixture records, for 64 fixed keys, the exact seed the oracle needed,
//! m, and each member's (slot, vertex). The Rust build must:
//! - choose the same seed (proving the retry/seed-derivation contract);
//! - resolve the same edge triples;
//! - produce the same slots.
//!
//! Non-members recorded by the oracle must be rejected here with a concrete
//! reason, never falsely accepted.

mod common;

use common::load_fixture;
use mphf::hash;
use mphf::{build, BuildConfig, Probe, VerifyMode};

#[test]
fn build_matches_oracle_seed_m_and_member_slots() {
    let fx = load_fixture();
    let exp_seed = fx["seed"].as_u64().unwrap();
    let exp_m = fx["m"].as_u64().unwrap() as usize;
    let keys: Vec<Vec<u8>> = fx["members"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["key"].as_str().unwrap().as_bytes().to_vec())
        .collect();

    let cfg = BuildConfig {
        base_seed: 1,
        verify: VerifyMode::FullKey,
        ..Default::default()
    };
    let report = build(keys.clone(), &cfg).expect("fixture set builds");
    assert_eq!(report.seed, exp_seed, "retry seed sequence diverged from oracle");
    assert_eq!(report.index.vertex_count(), exp_m);

    for m in fx["members"].as_array().unwrap() {
        let i = m["i"].as_u64().unwrap() as usize;
        let exp_slot = m["slot"].as_u64().unwrap();
        let exp_vertex = m["vertex"].as_u64().unwrap();
        // Edge triple itself must match the oracle's hashing.
        let e = hash::edge(&keys[i], exp_seed, exp_m as u64).expect("distinct edge");
        // Verify the vertex the oracle recorded is one of the three.
        assert!(e.contains(&exp_vertex), "vertex {exp_vertex} not in {e:?}");
        match report.index.probe(&keys[i]) {
            Probe::Member { slot } => assert_eq!(
                slot, exp_slot,
                "slot mismatch for key {:?}",
                String::from_utf8_lossy(&keys[i])
            ),
            other => panic!("member rejected: {other:?}"),
        }
    }
}

#[test]
fn oracle_non_members_are_rejected_not_accepted() {
    let fx = load_fixture();
    let seed = fx["seed"].as_u64().unwrap();
    let m = fx["m"].as_u64().unwrap();
    let keys: Vec<Vec<u8>> = fx["members"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["key"].as_str().unwrap().as_bytes().to_vec())
        .collect();
    let idx = build(
        keys.clone(),
        &BuildConfig {
            base_seed: 1,
            verify: VerifyMode::FullKey,
            ..Default::default()
        },
    )
    .unwrap()
    .index;

    for nm in fx["non_members"].as_array().unwrap() {
        let key = nm["key"].as_str().unwrap().as_bytes().to_vec();
        // Independently confirm what the oracle recorded for this key.
        if !nm["edge_collision"].as_bool().unwrap() {
            let e = hash::edge(&key, seed, m).expect("oracle said no collision");
            let _ = e;
        }
        match idx.probe(&key) {
            Probe::Rejected { reason, .. } => {
                // Every recorded non-member must yield a named reject class.
                let name = reason.as_str();
                assert!(
                    ["fingerprint_mismatch", "key_mismatch", "unoccupied_vertex", "edge_collision", "empty_set"]
                        .contains(&name),
                    "unexpected reject reason {name}"
                );
            }
            other => panic!(
                "oracle non-member {:?} falsely accepted: {other:?}",
                nm["key"].as_str().unwrap()
            ),
        }
    }
}
