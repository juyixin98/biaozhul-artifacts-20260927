//! Evidence suite #4: the Axum validation/operation HTTP interface.
//!
//! Drives the real router in-process via `tower::ServiceExt::oneshot` (no TCP, no
//! mocks): happy-path compression/listing/raw retrieval and the four distinguishable
//! failure classes expressed as HTTP status + stable category strings.

mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use base64::Engine;
use common::*;
use lz77_blocks::service::app;
use lz77_blocks::store::BlockStore;
use serde_json::{json, Value};
use tower::ServiceExt;

mod tempfile_lite {
    use std::fs;
    use std::path::PathBuf;
    pub struct TempDir {
        path: PathBuf,
    }
    impl TempDir {
        pub fn new(tag: &str) -> Self {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("target/test-tmp/http-stores")
                .join(format!("{tag}-{}-{nanos}", std::process::id()));
            fs::create_dir_all(&path).unwrap();
            Self { path }
        }
        pub fn path(&self) -> &std::path::Path {
            &self.path
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            if std::env::var_os("LZ77_KEEP_TMP").is_none() {
                let _ = fs::remove_dir_all(&self.path);
            }
        }
    }
}

async fn body_json(resp: axum::response::Response) -> (StatusCode, Value) {
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 4 << 20)
        .await
        .unwrap();
    let v = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, v)
}

async fn body_raw(resp: axum::response::Response) -> (StatusCode, Vec<u8>) {
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 16 << 20)
        .await
        .unwrap();
    (status, bytes.to_vec())
}

fn post_json(uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap()
}

fn b64(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

#[tokio::test]
async fn happy_path_compress_store_list_decompress() {
    let mut log = TestLog::new("service_api");
    let mut failures: Vec<String> = Vec::new();
    let dir = tempfile_lite::TempDir::new("happy");
    let router = app(BlockStore::open(dir.path()).unwrap());

    // Health on empty store.
    let (s, h) = body_json(
        router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap(),
    )
    .await;
    if s == StatusCode::OK && h["blocks"] == json!(0) {
        log.pass(
            "health_empty",
            "health reports zero blocks",
            json!({"body": h}),
        );
    } else {
        failures.push("health".into());
        log.fail("health_empty", "bad health", json!({"status": s.as_u16()}));
    }

    // Independent root.
    let root_data = b"alpha alpha alpha alpha alpha".to_vec();
    let (s, root) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/blocks",
                json!({"mode": "independent", "data": b64(&root_data)}),
            ))
            .await
            .unwrap(),
    )
    .await;
    let root_id = root["id"].as_str().unwrap().to_string();
    if s == StatusCode::CREATED && root["mode"] == "independent" {
        log.pass(
            "create_independent",
            "201 + metadata, payload smaller than data",
            json!({"status": s.as_u16(), "id": root_id,
                   "data_len": root["data_len"], "payload_len": root["payload_len"],
                   "compressed": root["payload_len"].as_u64() < root["data_len"].as_u64()}),
        );
    } else {
        failures.push("create_root".into());
        log.fail(
            "create_independent",
            "create failed",
            json!({"status": s.as_u16(), "body": root}),
        );
    }

    // Dependent child, pinning the root.
    let child_data = b"alpha alpha alpha continuation".to_vec();
    let (s, child) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/blocks",
                json!({"mode": "dependent", "data": b64(&child_data), "prev_id": root_id}),
            ))
            .await
            .unwrap(),
    )
    .await;
    let child_id = child["id"].as_str().unwrap_or("").to_string();
    if s == StatusCode::CREATED && child["prev_id"] == json!(root_id) {
        log.pass(
            "create_dependent",
            "201 dependent block bound to prev_id",
            json!({"id": child_id, "prev_id": child["prev_id"]}),
        );
    } else {
        failures.push("create_child".into());
        log.fail(
            "create_dependent",
            "create failed",
            json!({"status": s.as_u16(), "body": child}),
        );
    }

    // List.
    let (s, list) = body_json(
        router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/blocks")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap(),
    )
    .await;
    let ordered = s == StatusCode::OK
        && list.as_array().map(|a| a.len() == 2).unwrap_or(false)
        && list[0]["id"] == json!(root_id)
        && list[1]["id"] == json!(child_id);
    log.pass_or_record(
        ordered,
        "list_ordered",
        "two blocks listed root-first",
        json!({"list": list}),
    );
    if !ordered {
        failures.push("list".into());
    }

    // Per-block raw.
    let (s, raw) = body_raw(
        router
            .clone()
            .oneshot(
                Request::builder()
                    .uri(format!("/blocks/{child_id}/raw"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap(),
    )
    .await;
    if s == StatusCode::OK && raw == child_data {
        log.pass(
            "raw_child",
            "single-block raw returns exactly the child bytes",
            json!({"len": raw.len()}),
        );
    } else {
        failures.push("raw_child".into());
        log.fail("raw_child", "raw mismatch", json!({"status": s.as_u16()}));
    }

    // Chain raw == concatenation.
    let (s, chain) = body_raw(
        router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/chain/raw")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap(),
    )
    .await;
    let mut want = root_data.clone();
    want.extend_from_slice(&child_data);
    if s == StatusCode::OK && chain == want {
        log.pass(
            "chain_raw",
            "chain concatenation equals source order",
            json!({"len": chain.len()}),
        );
    } else {
        failures.push("chain_raw".into());
        log.fail("chain_raw", "mismatch", json!({"status": s.as_u16()}));
    }

    // Frame download round-trips through the independent reference.
    if oracle_available() {
        let (s, frame) = body_raw(
            router
                .clone()
                .oneshot(
                    Request::builder()
                        .uri(format!("/blocks/{child_id}/frame"))
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        let (rep, out) = oracle_roundtrip(&frame, &root_data);
        if rep.valid() && out.as_deref() == Some(child_data.as_slice()) {
            log.pass(
                "frame_oracle",
                "downloaded frame accepted by Python reference",
                json!({"oracle_stats": rep.json.get("stats")}),
            );
        } else {
            failures.push("frame_oracle".into());
            log.fail(
                "frame_oracle",
                "oracle rejected",
                json!({"report": rep.json}),
            );
        }
    }

    let path = log.finish();
    assert!(failures.is_empty(), "{failures:?}; log {path}");
}

#[tokio::test]
async fn failure_classes_map_to_distinct_status_and_category() {
    let mut log = TestLog::new("service_api");
    let mut failures: Vec<String> = Vec::new();
    let dir = tempfile_lite::TempDir::new("errors");
    let router = app(BlockStore::open(dir.path()).unwrap());

    // 400 input_error: bad JSON.
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/blocks")
                    .header("content-type", "application/json")
                    .body(Body::from("{not json"))
                    .unwrap(),
            )
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "bad_json",
        s == StatusCode::BAD_REQUEST && v["error"]["category"] == json!("input_error"),
        json!({"status": s.as_u16(), "body": v}),
    );

    // 400 input_error: malformed base64.
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/blocks",
                json!({"mode": "independent", "data": "@@@"}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "bad_base64",
        s == StatusCode::BAD_REQUEST && v["error"]["category"] == json!("input_error"),
        json!({"status": s.as_u16(), "detail": v["error"]["detail"]}),
    );

    // 409 state_conflict: dependent on empty store.
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/blocks",
                json!({"mode": "dependent", "data": b64(b"x")}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "dependent_empty",
        s == StatusCode::CONFLICT && v["error"]["category"] == json!("state_conflict"),
        json!({"status": s.as_u16(), "detail": v["error"]["detail"]}),
    );

    // Seed a root for the remaining cases.
    let (_, root) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/blocks",
                json!({"mode": "independent", "data": b64(b"root root root")}),
            ))
            .await
            .unwrap(),
    )
    .await;
    let root_id = root["id"].as_str().unwrap().to_string();

    // 409 stale pin after adding a child.
    let _ = router
        .clone()
        .oneshot(post_json(
            "/blocks",
            json!({"mode": "dependent", "data": b64(b"child"), "prev_id": root_id}),
        ))
        .await
        .unwrap();
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/blocks",
                json!({"mode": "dependent", "data": b64(b"late"), "prev_id": root_id}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "stale_pin",
        s == StatusCode::CONFLICT
            && v["error"]["category"] == json!("state_conflict")
            && v["error"]["detail"]
                .as_str()
                .unwrap()
                .contains("tip advanced"),
        json!({"status": s.as_u16()}),
    );

    // 404 not_found.
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/blocks/blk-00000099/raw")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "not_found",
        s == StatusCode::NOT_FOUND && v["error"]["category"] == json!("not_found"),
        json!({"status": s.as_u16()}),
    );

    // 413 resource_exhausted: validate the 1 GiB-bomb fixture through the endpoint.
    let bomb = fixture("malformed/bomb_declared_size.frame");
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/validate",
                json!({"frame": b64(&bomb), "mode": "independent"}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "validate_bomb_413",
        s == StatusCode::PAYLOAD_TOO_LARGE
            && v["valid"] == json!(false)
            && v["error_category"] == json!("resource_exhausted"),
        json!({"status": s.as_u16(), "reason": v["reason"]}),
    );

    // 400 via /validate for a CRC-corrupt frame.
    let crc_bad = fixture("malformed/bad_crc.frame");
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/validate",
                json!({"frame": b64(&crc_bad), "mode": "independent"}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "validate_bad_crc_400",
        s == StatusCode::BAD_REQUEST && v["error_category"] == json!("input_error"),
        json!({"status": s.as_u16(), "reason": v["reason"]}),
    );

    // Missing dictionary FIELD on a dependent validation: caller argument error.
    let dep = fixture("malformed/dependent_missing_predecessor.frame");
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/validate",
                json!({"frame": b64(&dep), "mode": "dependent"}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "validate_missing_dict_field_400",
        s == StatusCode::BAD_REQUEST && v["error"]["category"] == json!("input_error"),
        json!({"note": "missing 'dictionary' FIELD is caller input error",
               "status": s.as_u16(), "detail": v["error"]["detail"]}),
    );

    // And presenting an empty dictionary to a dependent frame is the state conflict.
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/validate",
                json!({"frame": b64(&dep), "mode": "dependent", "dictionary": b64(b"")}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "validate_digest_mismatch_409",
        s == StatusCode::CONFLICT && v["error_category"] == json!("state_conflict"),
        json!({"status": s.as_u16(), "reason": v["reason"]}),
    );

    // 200 valid validate.
    let good = fixture("good/self_overlap_rle.frame");
    let (s, v) = body_json(
        router
            .clone()
            .oneshot(post_json(
                "/validate",
                json!({"frame": b64(&good), "mode": "independent"}),
            ))
            .await
            .unwrap(),
    )
    .await;
    check(
        &mut log,
        &mut failures,
        "validate_good_200",
        s == StatusCode::OK && v["valid"] == json!(true) && v["data_len"] == json!(259),
        json!({"body": v}),
    );

    let path = log.finish();
    assert!(failures.is_empty(), "{failures:?}; log {path}");
}

fn check(log: &mut TestLog, failures: &mut Vec<String>, name: &str, ok: bool, state: Value) {
    log.pass_or_record(
        ok,
        name,
        if ok {
            "status code and error category match the contract"
        } else {
            "unexpected status/category"
        },
        state,
    );
    if !ok {
        failures.push(name.to_string());
    }
}
