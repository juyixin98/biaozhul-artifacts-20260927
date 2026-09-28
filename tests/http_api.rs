//! HTTP validation-interface tests.  Drives the Axum router in-process via
//! tower's ServiceExt (no real network port needed).

use rangecode::config::Config;
use rangecode::server::{b64_decode, b64_encode, router};
use rangecode::storage::FileStore;

use serde_json::Value;
use std::time::{SystemTime, UNIX_EPOCH};
use tower::util::ServiceExt;

use axum::body::Body;
use axum::http::{Request, StatusCode};

fn temp_store() -> (std::path::PathBuf, FileStore) {
    let dir = std::env::temp_dir().join(format!(
        "rangecode-http-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    (dir.clone(), FileStore::open(dir).unwrap())
}

fn test_config(root: &std::path::Path) -> Config {
    Config {
        bind_addr: "127.0.0.1:0".into(),
        storage_dir: root.to_path_buf(),
        frequency_bound: 1 << 14,
        alphabet: 256,
        chunk_target: 1024,
        max_symbols: 1_000_000,
        max_bytes: 16 << 20,
        max_alphabet: 512,
        expose_data: true,
    }
}

#[tokio::test]
async fn health_reports_ok() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let req = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
}

async fn call(app: axum::Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, json)
}

#[tokio::test]
async fn encode_decode_roundtrip_via_http() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);

    let payload = b"hello http range coder";
    let body = serde_json::json!({
        "data_base64": b64_encode(payload),
        "mode": "adaptive",
        "chunk_target": 8
    });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::OK, "encode body: {v}");
    assert_eq!(v["declared_symbols"], payload.len() as u64);
    let rid = v["request_id"].as_str().unwrap().to_string();
    assert!(!rid.is_empty());
    assert_eq!(v["diagnostics"]["decision"], "accepted");
    let container_b64 = v["container_base64"].as_str().unwrap().to_string();

    // Decode through the API.
    let (dir2, store2) = temp_store();
    let app2 = router(test_config(&dir2), store2);
    let body = serde_json::json!({ "container_base64": container_b64 });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/decode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app2, req).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["declared_symbols"], payload.len() as u64);
    let decoded = b64_decode(v["data_base64"].as_str().unwrap()).unwrap();
    assert_eq!(decoded, payload);
}

#[tokio::test]
async fn decode_of_garbage_returns_precise_error_kind() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let body = serde_json::json!({ "container_base64": b64_encode(&[0xffu8; 64]) });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/decode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(v["error_kind"].is_string());
    // 0xFF.. does not start with the RCMP magic.
    assert_eq!(v["error_kind"], "bad_magic");
    assert_eq!(v["diagnostics"]["decision"], "rejected");
    assert!(v["diagnostics"]["request_id"].is_string());
}

#[tokio::test]
async fn invalid_base64_is_bad_request() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let body = serde_json::json!({ "container_base64": "!!!not-base64!!!" });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/decode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error_kind"], "invalid_base64");
}

#[tokio::test]
async fn unknown_mode_is_bad_request() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let body = serde_json::json!({
        "data_base64": b64_encode(b"x"),
        "mode": "bogus"
    });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error_kind"], "unknown_mode");
}

#[tokio::test]
async fn request_id_is_honored_and_echoed() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let body = serde_json::json!({ "container_base64": b64_encode(&[1u8; 30]) });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/decode")
        .header("content-type", "application/json")
        .header("x-request-id", "corr-abc-123")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["request_id"], "corr-abc-123");
}

#[tokio::test]
async fn diagnostics_never_leak_raw_payload() {
    let (dir, store) = temp_store();
    let cfg = Config {
        expose_data: false,
        ..test_config(&dir)
    };
    let app = router(cfg, store);
    let secret = b"TOPSECRET-token-value-1234567890";
    let body = serde_json::json!({
        "data_base64": b64_encode(secret),
        "mode": "static"
    });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::OK);
    let rendered = v.to_string();
    // Response must not contain raw secret text (only base64 container,
    // which itself codes the secret but the diagnostics object must not).
    let diag = v["diagnostics"].to_string();
    assert!(!diag.contains("TOPSECRET"));
    // Fingerprint shows length + short hex windows only.
    assert_eq!(v["diagnostics"]["input"]["len"], secret.len() as u64);
    assert!(
        v["diagnostics"]["input"]["head_hex"]
            .as_str()
            .unwrap()
            .len()
            <= 16
    );
    let _ = rendered;
}

#[tokio::test]
async fn persistence_roundtrip_through_list_and_get() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let payload = b"persist me";
    let body = serde_json::json!({
        "data_base64": b64_encode(payload),
        "mode": "static",
        "id": "job-alpha"
    });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::OK, "{v}");

    let (dir2, store2) = (dir.clone(), FileStore::open(&dir).unwrap());
    let app2 = router(test_config(&dir2), store2);

    let req = Request::builder()
        .uri("/v1/jobs")
        .body(Body::empty())
        .unwrap();
    let (status, v) = call(app2, req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["jobs"][0], "job-alpha");

    let app3 = router(test_config(&dir), FileStore::open(&dir).unwrap());
    let req = Request::builder()
        .uri("/v1/jobs/job-alpha")
        .body(Body::empty())
        .unwrap();
    let (status, v) = call(app3, req).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let decoded = b64_decode(v["data_base64"].as_str().unwrap()).unwrap();
    assert_eq!(decoded, payload);
}

#[tokio::test]
async fn missing_job_is_404_style_storage_error() {
    let (dir, store) = temp_store();
    let app = router(test_config(&dir), store);
    let req = Request::builder()
        .uri("/v1/jobs/does-not-exist")
        .body(Body::empty())
        .unwrap();
    let (status, v) = call(app, req).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(v["error_kind"], "storage_io");
    assert_eq!(v["diagnostics"]["decision"], "indeterminate");
}
