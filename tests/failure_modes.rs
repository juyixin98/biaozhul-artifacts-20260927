//! Failure-mode tests. Each test asserts a concrete result and the specific
//! failure *category*, never just "the endpoint was callable".

mod common;

use common::*;
use ec_service::config::BUILTIN_PROFILES;

#[tokio::test]
async fn corrupt_shard_is_classified_corrupt_not_missing_and_treated_as_erasure() {
    let dir = unique_data_dir("corrupt-classify");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let data = b"classification test payload";
    state.put_object("obj", data, 3, 2).await.unwrap();

    flip_one_byte(&dir, "obj", 0); // data shard present, wrong digest
    let rep = state.inspect("obj").await.unwrap();
    assert_eq!(rep.status, "degraded_recoverable");
    assert_eq!(rep.corrupt_shards, vec![0]);
    assert!(rep.missing_shards.is_empty());
    // Bad shard is excluded like an erasure; the other k shards recover it.
    let got = state.get_object("obj").await.unwrap();
    assert_eq!(got, data);

    // Repair rewrites the corrupt file; post-verification proves it.
    let repair = state.repair("obj").await.unwrap();
    assert_eq!(repair.rebuilt_shards, vec![0]);
    assert!(repair.post_repair_verified);
    assert_eq!(state.inspect("obj").await.unwrap().status, "intact");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn corrupt_plus_missing_up_to_m_still_recovers() {
    let dir = unique_data_dir("mixed-erase");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let data = b"mixed loss: one missing, one corrupt";
    state.put_object("obj", data, 3, 2).await.unwrap();
    delete_shard_file(&dir, "obj", 2); // missing parity
    corrupt_shard_file(&dir, "obj", 1); // corrupted data shard
    let rep = state.inspect("obj").await.unwrap();
    assert_eq!(rep.missing_shards, vec![2]);
    assert_eq!(rep.corrupt_shards, vec![1]);
    assert!(rep.recoverable);
    assert_eq!(state.get_object("obj").await.unwrap(), data);
    let repair = state.repair("obj").await.unwrap();
    assert!(repair.post_repair_verified);
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn over_fault_tolerance_refuses_without_returning_bytes() {
    let dir = unique_data_dir("over-tol");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let data = b"cannot survive 3 losses with m=2";
    state.put_object("obj", data, 3, 2).await.unwrap();
    delete_shard_file(&dir, "obj", 0);
    delete_shard_file(&dir, "obj", 2);
    corrupt_shard_file(&dir, "obj", 4); // only shards 1,3 verify -> k-1
    let rep = state.inspect("obj").await.unwrap();
    assert_eq!(rep.status, "not_recoverable");
    assert!(!rep.recoverable);
    assert_eq!(rep.margin, -1);

    // GET must fail with the specific category and no payload.
    let err = state.get_object("obj").await.unwrap_err();
    assert_eq!(err.code, "NOT_RECOVERABLE");
    assert_eq!(err.status, axum::http::StatusCode::CONFLICT);
    let detail = err.detail;
    assert_eq!(detail["need"], 3);
    assert_eq!(detail["corrupt"], serde_json::json!([4]));

    // Repair must likewise refuse rather than fabricate.
    let err = state.repair("obj").await.unwrap_err();
    assert_eq!(err.code, "NOT_RECOVERABLE");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn kernel_rejects_duplicate_shard_indices() {
    // Duplicate shard numbers cannot silently masquerade as two distinct
    // shards: the kernel rejects them with a named category.
    let enc = ec_service::erasure::encode(b"dup test xyz", 3, 2).unwrap();
    let avail = vec![
        (1u8, enc.shards[1].clone()),
        (1u8, enc.shards[1].clone()),
        (2u8, enc.shards[2].clone()),
    ];
    let err = ec_service::erasure::reconstruct(&avail, 3, 2, enc.shard_len).unwrap_err();
    assert_eq!(err.code(), "DUPLICATE_OR_INVALID_INDEX");
    assert_eq!(
        err,
        ec_service::erasure::CodeError::DuplicateOrInvalidIndex { index: 1 }
    );
}

#[tokio::test]
async fn shard_length_mismatch_is_a_named_error() {
    let enc = ec_service::erasure::encode(b"length mismatch!!", 3, 2).unwrap();
    let mut bad = enc.shards[0].clone();
    bad.push(0x00);
    let avail = vec![
        (0u8, bad),
        (1u8, enc.shards[1].clone()),
        (2u8, enc.shards[2].clone()),
    ];
    let err = ec_service::erasure::reconstruct(&avail, 3, 2, enc.shard_len).unwrap_err();
    assert_eq!(err.code(), "SHARD_LENGTH_MISMATCH");
}

#[tokio::test]
async fn manifest_tampering_original_len_is_detected_and_object_untrusted() {
    let dir = unique_data_dir("manifest-len");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let data = b"length authentication matters";
    state.put_object("obj", data, 3, 2).await.unwrap();

    // Forge original_len in the manifest without resealing.
    let path = manifest_path(&dir, "obj");
    let mut j = serde_json::from_slice::<serde_json::Value>(&std::fs::read(&path).unwrap())
        .unwrap();
    j["original_len"] = 999.into();
    std::fs::write(&path, serde_json::to_vec_pretty(&j).unwrap()).unwrap();

    // Every data operation must refuse with a digest-mismatch category.
    assert_eq!(
        state.store.read_manifest("obj").await.unwrap_err().code,
        "MANIFEST_DIGEST_MISMATCH"
    );
    assert_eq!(state.inspect("obj").await.unwrap_err().code, "MANIFEST_DIGEST_MISMATCH");
    assert_eq!(state.get_object("obj").await.unwrap_err().code, "MANIFEST_DIGEST_MISMATCH");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn manifest_tampering_shard_checksum_is_detected() {
    let dir = unique_data_dir("manifest-sha");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    state.put_object("obj", b"checksum auth", 2, 1).await.unwrap();
    let path = manifest_path(&dir, "obj");
    let mut j = serde_json::from_slice::<serde_json::Value>(&std::fs::read(&path).unwrap())
        .unwrap();
    j["shards"][0]["sha256"] = "00".repeat(32).into();
    std::fs::write(&path, serde_json::to_vec_pretty(&j).unwrap()).unwrap();
    assert_eq!(
        state.get_object("obj").await.unwrap_err().code,
        "MANIFEST_DIGEST_MISMATCH"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn forged_manifest_with_reseal_still_blocked_by_version_check() {
    // An attacker who can recompute the digest cannot forge the version:
    // resealing a bumped format version hits UNSUPPORTED_VERSION.
    let dir = unique_data_dir("manifest-ver");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    state.put_object("obj", b"version gate", 2, 1).await.unwrap();
    let path = manifest_path(&dir, "obj");
    let bytes = std::fs::read(&path).unwrap();
    let mut m: ec_service::manifest::Manifest = serde_json::from_slice(&bytes).unwrap();
    m.algorithm.format_version = 7;
    ec_service::manifest::seal(&mut m); // reseal honestly over forged field
    std::fs::write(&path, serde_json::to_vec_pretty(&m).unwrap()).unwrap();
    let err = state.get_object("obj").await.unwrap_err();
    assert_eq!(err.code, "UNSUPPORTED_VERSION");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn padding_is_authenticated_and_stripped_exactly() {
    // Boundary lengths around k multiples for k=3: original sizes
    // 0,1,2,3,4 -> pad 0,2,1,0,2; returned payload must be exact, and a
    // forged pad_len must be rejected.
    let dir = unique_data_dir("padding");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    for len in 0..=6usize {
        let oid = format!("p{len}");
        let data = vec![0xC0 + len as u8; len];
        state.put_object(&oid, &data, 3, 2).await.unwrap();
        assert_eq!(state.get_object(&oid).await.unwrap(), data);
        let j = read_manifest_json(&dir, &oid);
        let shard_len = j["shard_len"].as_u64().unwrap();
        assert_eq!(shard_len * 3 - j["original_len"].as_u64().unwrap(), j["pad_len"].as_u64().unwrap());
    }
    // Forge pad_len (and reseal): structural consistency must reject it.
    let path = manifest_path(&dir, "p1");
    let mut m: ec_service::manifest::Manifest =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    m.pad_len = 0; // inconsistent with k*shard_len - original_len
    ec_service::manifest::seal(&mut m);
    std::fs::write(&path, serde_json::to_vec_pretty(&m).unwrap()).unwrap();
    let err = state.get_object("p1").await.unwrap_err();
    assert_eq!(err.code, "MANIFEST_INCONSISTENT");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn unknown_object_and_bad_inputs_return_named_errors() {
    let dir = unique_data_dir("inputs");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    assert_eq!(state.inspect("nope").await.unwrap_err().code, "OBJECT_NOT_FOUND");
    assert_eq!(state.get_object("nope").await.unwrap_err().code, "OBJECT_NOT_FOUND");
    assert_eq!(state.repair("nope").await.unwrap_err().code, "OBJECT_NOT_FOUND");

    // disallowed profile
    let err = state.put_object("x", b"d", 9, 9).await.unwrap_err();
    assert_eq!(err.code, "PROFILE_NOT_ALLOWED");
    // invalid params entirely (k+m > 255)
    assert!(matches!(
        ec_service::erasure::validate_params(200, 200).unwrap_err().code(),
        "BAD_PARAMS"
    ));
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn truncation_corruption_is_classified_as_corrupt() {
    // A shard file with the wrong size is corrupt (distinguishable from a
    // completely missing file).
    let dir = unique_data_dir("trunc");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    state.put_object("obj", b"truncate me please now", 3, 2).await.unwrap();
    truncate_shard_file(&dir, "obj", 3);
    let rep = state.inspect("obj").await.unwrap();
    assert_eq!(rep.corrupt_shards, vec![3]);
    assert!(rep.missing_shards.is_empty());
    assert_eq!(
        state.get_object("obj").await.unwrap(),
        b"truncate me please now"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
