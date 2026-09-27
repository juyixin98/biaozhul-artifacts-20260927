//! HTTP end-to-end tests against the real Axum server over TCP using a tiny
//! std-only HTTP client (no test-only web framework dependency).

mod common;

use common::*;
use ec_service::config::BUILTIN_PROFILES;

#[tokio::test]
async fn full_http_lifecycle_encode_inspect_corrupt_repair_download() {
    let dir = unique_data_dir("http-life");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let addr = spawn_server(state).await;

    // health
    let r = http_request(&addr, "GET", "/healthz", &[], None).await.unwrap();
    assert_eq!(r.status, 200);
    let j = r.json();
    assert_eq!(j["field"], "GF256-PP0x11D-G2/v1");
    assert!(r.header("x-request-id").is_some(), "every response carries request id");

    // config
    let r = http_request(&addr, "GET", "/v1/config", &[], None).await.unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.json()["format_version"], 1);

    // encode
    let payload = b"http lifecycle payload 0123456789";
    let r = http_request(
        &addr,
        "PUT",
        "/v1/objects/doc1?k=3&m=2",
        payload,
        Some("application/octet-stream"),
    )
    .await
    .unwrap();
    assert_eq!(r.status, 201);
    let put = r.json();
    assert_eq!(put["ok"], true);
    assert_eq!(put["k"], 3);
    assert_eq!(put["m"], 2);
    assert_eq!(put["original_len"], payload.len() as u64);
    let req_id = r.header("x-request-id").unwrap().to_string();

    // inbound request id is honored for correlation
    let r2 = common::http_request_ex(
        &addr,
        "GET",
        "/v1/objects/doc1/inspect",
        &[],
        None,
        Some(&format!("x-request-id: {req_id}")),
    )
    .await
    .unwrap();
    assert_eq!(r2.status, 200);
    assert_eq!(r2.header("x-request-id").unwrap(), req_id);
    assert_eq!(r2.json()["status"], "intact");

    // lose two shards on disk, then observe via HTTP
    delete_shard_file(&dir, "doc1", 0);
    corrupt_shard_file(&dir, "doc1", 4);

    let r = http_request(&addr, "GET", "/v1/objects/doc1/inspect", &[], None).await.unwrap();
    assert_eq!(r.status, 200);
    let insp = r.json();
    assert_eq!(insp["status"], "degraded_recoverable");
    assert_eq!(insp["missing_shards"], serde_json::json!([0]));
    assert_eq!(insp["corrupt_shards"], serde_json::json!([4]));
    // per-shard explainability: expected vs computed digest for bad shard
    let bad = insp["shards"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["index"] == 4)
        .unwrap();
    assert_eq!(bad["status"], "corrupt");
    assert!(bad["expected_sha256"].is_string());
    assert!(bad["computed_sha256"].is_string());
    assert_ne!(bad["expected_sha256"], bad["computed_sha256"]);

    // download still returns exact bytes
    let r = http_request(&addr, "GET", "/v1/objects/doc1", &[], None).await.unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.body, payload);
    assert_eq!(r.header("x-payload-sha256").unwrap(), put["payload_sha256"].as_str().unwrap());
    assert_eq!(r.header("x-object-status").unwrap(), "degraded_recoverable");

    // repair
    let r = http_request(&addr, "POST", "/v1/objects/doc1/repair", &[], None).await.unwrap();
    assert_eq!(r.status, 200);
    let rep = r.json();
    assert_eq!(rep["status_after"], "intact");
    assert_eq!(rep["post_repair_verified"], true);
    let rebuilt = rep["rebuilt_shards"].as_array().unwrap();
    assert_eq!(rebuilt.len(), 2);

    let r = http_request(&addr, "GET", "/v1/objects/doc1", &[], None).await.unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.header("x-object-status").unwrap(), "intact");
    assert_eq!(r.body, payload);

    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn http_below_k_returns_explained_conflict_without_data() {
    let dir = unique_data_dir("http-below");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let addr = spawn_server(state).await;
    let payload = b"xyz";
    let r = http_request(&addr, "PUT", "/v1/objects/d?k=2&m=1", payload, None).await.unwrap();
    assert_eq!(r.status, 201);

    // wipe all but one shard -> below k
    delete_shard_file(&dir, "d", 0);
    delete_shard_file(&dir, "d", 2);

    let r = http_request(&addr, "GET", "/v1/objects/d", &[], None).await.unwrap();
    assert_eq!(r.status, 409);
    let j = r.json();
    assert_eq!(j["ok"], false);
    assert_eq!(j["error"]["code"], "NOT_RECOVERABLE");
    let msg = j["error"]["message"].as_str().unwrap();
    assert!(
        msg.contains("fewer than k") && msg.contains("no data"),
        "message must explain that no data is returned, got: {msg}"
    );
    assert_eq!(j["error"]["detail"]["need"], 2);
    // No fabricated payload: body is the JSON error, not raw bytes.
    assert!(serde_json::from_slice::<serde_json::Value>(&r.body).is_ok());

    let r = http_request(&addr, "POST", "/v1/objects/d/repair", &[], None).await.unwrap();
    assert_eq!(r.status, 409);
    assert_eq!(r.json()["error"]["code"], "NOT_RECOVERABLE");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn http_manifest_tamper_and_missing_object_errors() {
    let dir = unique_data_dir("http-errors");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let addr = spawn_server(state).await;
    let r = http_request(&addr, "PUT", "/v1/objects/m?k=2&m=1", b"ab", None).await.unwrap();
    assert_eq!(r.status, 201);

    // tamper manifest
    let path = manifest_path(&dir, "m");
    let mut j: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    j["original_len"] = 5.into();
    std::fs::write(&path, serde_json::to_vec_pretty(&j).unwrap()).unwrap();
    let r = http_request(&addr, "GET", "/v1/objects/m", &[], None).await.unwrap();
    assert_eq!(r.status, 422);
    assert_eq!(r.json()["error"]["code"], "MANIFEST_DIGEST_MISMATCH");

    // missing object
    let r = http_request(&addr, "GET", "/v1/objects/ghost/inspect", &[], None).await.unwrap();
    assert_eq!(r.status, 404);
    assert_eq!(r.json()["error"]["code"], "OBJECT_NOT_FOUND");

    // invalid object ids: a raw client that does not normalize dot segments
    // reaches the handler, which rejects ".." with INVALID_OBJECT_ID (400);
    // an encoded slash is rejected the same way.
    let r = http_request(&addr, "PUT", "/v1/objects/..?k=2&m=1", b"x", None).await.unwrap();
    assert_eq!(r.status, 400);
    assert_eq!(r.json()["error"]["code"], "INVALID_OBJECT_ID");
    let r = http_request(&addr, "PUT", "/v1/objects/a%2Fb?k=2&m=1", b"x", None).await.unwrap();
    assert_eq!(r.status, 400);
    assert_eq!(r.json()["error"]["code"], "INVALID_OBJECT_ID");

    // duplicate object
    let r = http_request(&addr, "PUT", "/v1/objects/m?k=2&m=1", b"zz", None).await.unwrap();
    assert_eq!(r.status, 409);
    assert_eq!(r.json()["error"]["code"], "OBJECT_ALREADY_EXISTS");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn http_single_byte_corruption_in_each_shard_is_repaired() {
    // Single-shard corruption sweep: flip one byte in each shard position,
    // one position at a time; every case must classify, recover exactly and
    // repair.
    let dir = unique_data_dir("http-sweep");
    let state = state_for(&dir, BUILTIN_PROFILES.to_vec()).await;
    let addr = spawn_server(state).await;
    let payload = b"single byte corruption sweep over all shard slots!!";
    let _ = http_request(&addr, "PUT", "/v1/objects/s?k=4&m=2", payload, None).await.unwrap();

    for idx in 0u8..6 {
        flip_one_byte(&dir, "s", idx);
        let r = http_request(&addr, "GET", "/v1/objects/s/inspect", &[], None).await.unwrap();
        let j = r.json();
        assert_eq!(j["status"], "degraded_recoverable", "shard {idx}");
        assert_eq!(j["corrupt_shards"], serde_json::json!([idx]));
        let r = http_request(&addr, "GET", "/v1/objects/s", &[], None).await.unwrap();
        assert_eq!(r.status, 200, "shard {idx}");
        assert_eq!(r.body, payload, "shard {idx}");
        let r = http_request(&addr, "POST", "/v1/objects/s/repair", &[], None).await.unwrap();
        assert_eq!(r.json()["status_after"], "intact", "shard {idx}");
    }
    let _ = std::fs::remove_dir_all(&dir);
}
