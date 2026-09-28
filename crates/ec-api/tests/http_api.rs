//! End-to-end HTTP tests against the real Axum router backed by an in-memory
//! store (same [`ObjectStore`] semantics as the filesystem adapter).
//!
//! These assert concrete results and exact failure categories:
//! - full encode → verify → decode round trip;
//! - missing vs digest-bad shards are reported under different names;
//! - every recoverable erasure combination recovers the same bytes;
//! - beyond-tolerance loss returns 409 INSUFFICIENT_SHARDS and NO data field;
//! - duplicate shard index is rejected (stateless path);
//! - manifest tampering returns MANIFEST_DIGEST_MISMATCH;
//! - single-shard corruption is healed as an erasure;
//! - repair persists rebuilt shards;
//! - request id correlation: supplied header is echoed.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use ec_api::http::AppState;
use ec_api::{router, ErasureCodingService};
use ec_core::CodecConfig;
use ec_store::{MemoryStore, ObjectStore, ShardRead};
use tower::util::ServiceExt;

/// Store handle plus the raw encoded shard bytes a test needs for stateless
/// requests.
struct Harness {
    app: axum::Router,
    store: Arc<MemoryStore>,
}

fn harness() -> Harness {
    let store = Arc::new(MemoryStore::new());
    let svc = ErasureCodingService::new(store.clone());
    let app = router(AppState {
        service: svc,
        cfg: CodecConfig::new(3, 2).unwrap(),
        allow_writes: true,
    });
    Harness { app, store }
}

async fn send(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<serde_json::Value>,
    request_id: Option<&str>,
) -> (StatusCode, serde_json::Value, Option<String>) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(id) = request_id {
        builder = builder.header("x-request-id", id);
    }
    let req = match body {
        Some(v) => builder
            .header("content-type", "application/json")
            .body(Body::from(v.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let echoed = resp
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| {
        serde_json::json!({"raw": String::from_utf8_lossy(&bytes).to_string()})
    });
    (status, json, echoed)
}

async fn encode_object(h: &Harness, data: &[u8], id: &str) -> serde_json::Value {
    let payload = serde_json::json!({"object_id": id, "data_b64": B64.encode(data)});
    let (status, json, _) = send(&h.app, "POST", "/v1/objects", Some(payload), None).await;
    assert_eq!(status, StatusCode::OK, "encode failed: {json}");
    json["result"].clone()
}

fn combinations(n: usize, r: usize) -> Vec<Vec<usize>> {
    fn go(n: usize, start: usize, r: usize, cur: &mut Vec<usize>, out: &mut Vec<Vec<usize>>) {
        if cur.len() == r {
            out.push(cur.clone());
            return;
        }
        for i in start..n {
            cur.push(i);
            go(n, i + 1, r, cur, out);
            cur.pop();
        }
    }
    let mut out = Vec::new();
    go(n, 0, r, &mut Vec::new(), &mut out);
    out
}

/// Persisted shard bytes for an object (memory store test API reconstructs
/// them by reading every index).
async fn stored_shards(h: &Harness, id: &str, n: usize) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    for i in 0..n {
        match h.store.read_shard(id, i as u16).unwrap() {
            ShardRead::Present(b) => out.push(b),
            ShardRead::Missing => panic!("shard {i} unexpectedly missing"),
        }
    }
    out
}

#[tokio::test]
async fn health_and_request_id_correlation() {
    let h = harness();
    let (status, json, echoed) = send(&h.app, "GET", "/health", None, Some("req-trace-123")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["status"], "ok");
    assert_eq!(echoed.as_deref(), Some("req-trace-123"));

    // A generated id is still echoed and present in the body.
    let (status, _, echoed) = send(&h.app, "GET", "/health", None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(echoed.unwrap().len() >= 16);
}

#[tokio::test]
async fn encode_verify_decode_round_trip_reports_exact_layout() {
    let h = harness();
    let data = b"0123456789abcdef0123456789abcdef0123456789"; // exactly 42 bytes
    let result = encode_object(&h, data, "obj-round").await;
    assert_eq!(result["k"], 3);
    assert_eq!(result["m"], 2);
    assert_eq!(result["n"], 5);
    assert_eq!(result["original_len"], 42);
    assert_eq!(result["shard_len"], 14); // ceil(42/3)
    assert_eq!(result["pad_len"], 0);
    assert_eq!(result["field_primitive"], "GF2P8-0x11B-G3");
    assert!(result["manifest_digest_hex"].as_str().unwrap().len() == 64);

    let (status, v, _) = send(&h.app, "GET", "/v1/objects/obj-round/verify", None, None).await;
    assert_eq!(status, StatusCode::OK);
    let r = &v["result"];
    assert_eq!(r["manifest_digest_ok"], true);
    assert_eq!(r["good_count"], 5);
    assert_eq!(r["recoverable"], true);
    assert_eq!(r["format_version"], 1);
    assert!(r["steps"].as_array().unwrap().len() >= 3);

    let (status, d, rid) =
        send(&h.app, "POST", "/v1/objects/obj-round/decode", None, Some("rid-42")).await;
    assert_eq!(status, StatusCode::OK, "{d}");
    assert_eq!(d["request_id"], "rid-42");
    assert_eq!(rid.as_deref(), Some("rid-42"));
    let got = B64.decode(d["result"]["data_b64"].as_str().unwrap()).unwrap();
    assert_eq!(got, data);
    // Systematic: with all shards present, the first k are the data rows.
    assert_eq!(d["result"]["used_shard_indices"], serde_json::json!([0, 1, 2]));
}

#[tokio::test]
async fn non_aligned_padding_is_authenticated_and_removed_on_decode() {
    let h = harness();
    let data = b"X".repeat(31); // k=3 -> shard_len 11, pad 2
    encode_object(&h, &data, "obj-pad").await;
    let (_, d, _) = send(&h.app, "POST", "/v1/objects/obj-pad/decode", None, None).await;
    let got = B64.decode(d["result"]["data_b64"].as_str().unwrap()).unwrap();
    assert_eq!(got.len(), 31);
    assert_eq!(got, data);
    assert_eq!(d["result"]["verify"]["pad_len"], 2);
    assert_eq!(d["result"]["original_len"], 31);
}

#[tokio::test]
async fn missing_and_bad_digest_are_distinct_and_both_treated_as_erasures() {
    let h = harness();
    let data = b"erasure-classification-fixture!!"; // 30 bytes
    encode_object(&h, data, "obj-class").await;

    h.store.test_remove_shard("obj-class", 1); // missing data shard
    h.store.test_corrupt_shard("obj-class", 4); // corrupted parity shard

    let (status, v, _) = send(&h.app, "GET", "/v1/objects/obj-class/verify", None, None).await;
    assert_eq!(status, StatusCode::OK);
    let r = &v["result"];
    assert_eq!(r["missing_indices"], serde_json::json!([1]));
    assert_eq!(r["bad_digest_indices"], serde_json::json!([4]));
    assert_eq!(r["shards"]["1"], "missing");
    assert_eq!(r["shards"]["4"], "bad_digest");
    assert_eq!(r["good_count"], 3);
    assert_eq!(r["recoverable"], true);

    let (_, d, _) = send(&h.app, "POST", "/v1/objects/obj-class/decode", None, None).await;
    let got = B64.decode(d["result"]["data_b64"].as_str().unwrap()).unwrap();
    assert_eq!(got, data);
    let used: Vec<u64> = d["result"]["used_shard_indices"]
        .as_array()
        .unwrap()
        .iter().map(|x| x.as_u64().unwrap()).collect();
    assert!(!used.contains(&1));
    assert!(!used.contains(&4));
}

#[tokio::test]
async fn every_recoverable_missing_combination_recovers_over_http() {
    // All 16 erasure patterns of size 0..=m for k=3,m=2.
    let h = harness();
    let data = b"exhaustive-http-input-patterns!"; // 31 bytes, pad 2
    let mut checked = 0usize;
    for count in 0..=2usize {
        for missing in combinations(5, count) {
            let oid = format!(
                "obj-exh-{count}-{}",
                missing.iter().map(|x| x.to_string()).collect::<Vec<_>>().join("_")
            );
            encode_object(&h, data, &oid).await;
            for &i in &missing {
                h.store.test_remove_shard(&oid, i as u16);
            }
            let (status, d, _) =
                send(&h.app, "POST", &format!("/v1/objects/{oid}/decode"), None, None).await;
            assert_eq!(status, StatusCode::OK, "missing {missing:?}: {d}");
            let got = B64.decode(d["result"]["data_b64"].as_str().unwrap()).unwrap();
            assert_eq!(got, data, "missing {missing:?}");
            checked += 1;
        }
    }
    assert_eq!(checked, 1 + 5 + 10);
}

#[tokio::test]
async fn beyond_tolerance_returns_conflict_and_no_data_field() {
    let h = harness();
    let data = b"too many shards are gone now!!"; // 29 bytes
    encode_object(&h, data, "obj-lost").await;
    for i in [0u16, 2, 4] {
        h.store.test_remove_shard("obj-lost", i);
    }
    let (status, d, _) = send(&h.app, "POST", "/v1/objects/obj-lost/decode", None, None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(d["ok"], false);
    assert_eq!(d["error"]["code"], "INSUFFICIENT_SHARDS");
    let msg = d["error"]["message"].as_str().unwrap();
    assert!(msg.contains("2 usable shard(s)") && msg.contains("3 required"), "{msg}");
    // No fabricated payload anywhere.
    assert!(d.pointer("/result/data_b64").is_none());
    assert!(d.get("data_b64").is_none());

    // include_data=false on a healthy object also honours the no-data request.
    let h2 = harness();
    encode_object(&h2, b"hello world bytes", "obj-nd").await;
    let (status, d, _) =
        send(&h2.app, "POST", "/v1/objects/obj-nd/decode?include_data=false", None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(d["result"].get("data_b64").is_none());
    assert_eq!(d["result"]["recovered"], true);
}

#[tokio::test]
async fn duplicate_shard_index_stateless_is_rejected_with_exact_code() {
    let h = harness();
    let data = b"stateless duplicate index probe";
    encode_object(&h, data, "obj-dup").await;
    let manifest_json = h.store.test_manifest_json("obj-dup").unwrap();
    let manifest: serde_json::Value = serde_json::from_str(&manifest_json).unwrap();
    let shards = stored_shards(&h, "obj-dup", 5).await;
    let input = |i: usize| serde_json::json!({"index": i, "data_b64": B64.encode(&shards[i])});
    let payload = serde_json::json!({
        "manifest_json": manifest,
        "shards": [input(0), input(0), input(1)],
    });
    let (status, d, _) = send(&h.app, "POST", "/v1/decode", Some(payload), None).await;
    assert_eq!(status, StatusCode::CONFLICT, "{d}");
    assert_eq!(d["error"]["code"], "DUPLICATE_SHARD_INDEX");
}

#[tokio::test]
async fn stateless_decode_with_golden_shards_recovers_and_refuses_when_short() {
    let h = harness();
    let data = b"stateless happy path payload here"; // 32 bytes
    encode_object(&h, data, "obj-sl").await;
    let manifest: serde_json::Value =
        serde_json::from_str(&h.store.test_manifest_json("obj-sl").unwrap()).unwrap();
    let shards = stored_shards(&h, "obj-sl", 5).await;
    let input = |i: usize| serde_json::json!({"index": i, "data_b64": B64.encode(&shards[i])});

    // Any k distinct shards decode statelessly (choose parity-heavy set 2,3,4).
    let payload = serde_json::json!({
        "manifest_json": manifest.clone(),
        "shards": [input(2), input(3), input(4)],
    });
    let (status, d, _) = send(&h.app, "POST", "/v1/decode", Some(payload), None).await;
    assert_eq!(status, StatusCode::OK, "{d}");
    let got = B64.decode(d["result"]["data_b64"].as_str().unwrap()).unwrap();
    assert_eq!(got, data);

    // Only 2 shards -> 409 INSUFFICIENT_SHARDS, no data.
    let payload = serde_json::json!({
        "manifest_json": manifest,
        "shards": [input(3), input(4)],
    });
    let (status, d, _) = send(&h.app, "POST", "/v1/decode", Some(payload), None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(d["error"]["code"], "INSUFFICIENT_SHARDS");
    assert!(d.pointer("/result/data_b64").is_none());
}

#[tokio::test]
async fn tampered_manifest_stateless_is_unprocessable() {
    let h = harness();
    let data = b"012345678901234567890123456789"; // exactly 30 bytes
    encode_object(&h, data, "obj-mt").await;
    let mut v: serde_json::Value =
        serde_json::from_str(&h.store.test_manifest_json("obj-mt").unwrap()).unwrap();
    // Data is 30 bytes, shard_len=10 (capacity 30). Keep the arithmetic
    // internally consistent (original_len 29 + pad_len 1 = 30) so that
    // structural validation passes and the failure is unambiguously the
    // manifest digest — proving original_len is in the covered set.
    v["original_len"] = serde_json::json!(29);
    v["pad_len"] = serde_json::json!(1);
    let payload = serde_json::json!({"manifest_json": v, "shards": []});
    let (status, d, _) = send(&h.app, "POST", "/v1/decode", Some(payload), None).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(d["error"]["code"], "MANIFEST_DIGEST_MISMATCH");
}

#[tokio::test]
async fn single_corrupted_shard_recovers_and_repair_persists() {
    let h = harness();
    let data = b"repair flow demonstration data"; // 29 bytes
    encode_object(&h, data, "obj-rep").await;
    h.store.test_corrupt_shard("obj-rep", 3);

    let (status, d, _) = send(&h.app, "POST", "/v1/objects/obj-rep/decode", None, None).await;
    assert_eq!(status, StatusCode::OK, "{d}");
    assert_eq!(d["result"]["verify"]["bad_digest_indices"], serde_json::json!([3]));
    let got = B64.decode(d["result"]["data_b64"].as_str().unwrap()).unwrap();
    assert_eq!(got, data);

    let payload = serde_json::json!({"object_id": "obj-rep", "targets": [3]});
    let (status, r, _) =
        send(&h.app, "POST", "/v1/objects/obj-rep/repair", Some(payload), None).await;
    assert_eq!(status, StatusCode::OK, "{r}");
    assert_eq!(r["result"]["repaired"], true);
    assert!(r["result"]["rebuilt_shards_b64"]["3"].is_string());

    let (_, v, _) = send(&h.app, "GET", "/v1/objects/obj-rep/verify", None, None).await;
    assert_eq!(v["result"]["good_count"], 5);
    // On re-verification the repaired shard is simply "good" again; the
    // "rebuilt" classification is specific to the repair response itself
    // (see r["rebuilt_shards_b64"] asserted above).
    assert_eq!(v["result"]["shards"]["3"], "good");
    assert_eq!(v["result"]["bad_digest_indices"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn auto_repair_targets_every_missing_shard() {
    let h = harness();
    let data = b"auto targets repair flow test!"; // 29 bytes
    encode_object(&h, data, "obj-auto").await;
    h.store.test_remove_shard("obj-auto", 0);
    h.store.test_remove_shard("obj-auto", 4);
    // Empty targets => repair everything missing/bad.
    let payload = serde_json::json!({"object_id": "obj-auto", "targets": []});
    let (status, r, _) =
        send(&h.app, "POST", "/v1/objects/obj-auto/repair", Some(payload), None).await;
    assert_eq!(status, StatusCode::OK, "{r}");
    assert!(r["result"]["rebuilt_shards_b64"]["0"].is_string());
    assert!(r["result"]["rebuilt_shards_b64"]["4"].is_string());
    let (_, v, _) = send(&h.app, "GET", "/v1/objects/obj-auto/verify", None, None).await;
    assert_eq!(v["result"]["good_count"], 5);
}

#[tokio::test]
async fn repair_with_too_few_shards_is_refused() {
    let h = harness();
    let data = b"cannot repair with only 2 of 5";
    encode_object(&h, data, "obj-rep2").await;
    for i in [1u16, 3, 4] {
        h.store.test_remove_shard("obj-rep2", i);
    }
    let payload = serde_json::json!({"object_id": "obj-rep2", "targets": [1, 3, 4]});
    let (status, r, _) =
        send(&h.app, "POST", "/v1/objects/obj-rep2/repair", Some(payload), None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(r["error"]["code"], "INSUFFICIENT_SHARDS");
}

#[tokio::test]
async fn unknown_object_and_bad_endpoint_are_distinct_404s() {
    let h = harness();
    let (status, d, _) =
        send(&h.app, "GET", "/v1/objects/nope/verify", None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(d["error"]["code"], "STORE_ERROR"); // NOT_FOUND carried by Store
    let (status, _, _) = send(&h.app, "GET", "/no/such/route", None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}
