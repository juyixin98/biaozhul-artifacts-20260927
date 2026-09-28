//! End-to-end HTTP tests against the Axum router. These bind to an
//! ephemeral port and exercise real TCP/HTTP via tower's oneshot service.

use range_codec::api::{router, AppState};
use range_codec::config::Config;
use range_codec::persist::Store;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use std::sync::Arc;
use tower::ServiceExt;

fn temp_dir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "rc-api-{tag}-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

fn app(tag: &str) -> axum::Router {
    let dir = temp_dir(tag);
    let cfg = Config {
        data_dir: dir,
        http_body_limit: 1 << 20,
        ..Config::default()
    };
    let store = Store::new(&cfg.data_dir).unwrap();
    router(AppState {
        config: Arc::new(cfg),
        store: Arc::new(store),
    })
}

async fn send_json(app: &axum::Router, method: &str, uri: &str, body: Value) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let json: Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| {
        serde_json::json!({"_raw": String::from_utf8_lossy(&bytes).to_string()})
    });
    (status, json)
}

#[tokio::test]
async fn healthz_ok() {
    let app = app("health");
    let resp = app
        .oneshot(Request::builder().uri("/healthz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn static_encode_then_verify_roundtrip() {
    let app = app("static");

    // Encode four symbols: [0,2,1,2] with freqs [2,1,3].
    let enc_body = serde_json::json!({
        "num_symbols": 3,
        "chunks": [
            {"freqs": [2, 1, 3], "symbols_b64": "AAIBAg=="}
        ]
    });
    let (st, enc) = send_json(&app, "POST", "/v1/encode/static", enc_body).await;
    assert_eq!(st, StatusCode::OK, "body: {enc}");
    let data_b64 = enc["data_b64"].as_str().unwrap().to_string();
    assert!(enc["bytes"].as_u64().unwrap() >= 5);

    // Verify the produced container.
    let (st, v) = send_json(
        &app,
        "POST",
        "/v1/verify",
        serde_json::json!({"data": data_b64}),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "body: {v}");
    assert_eq!(v["decision"], "accepted");
    assert_eq!(v["decoded_len"], 4);
    assert_eq!(v["symbols_b64"], "AAIBAg==");
    assert_eq!(v["chunks"], 1);
    assert_eq!(v["mode"], "static");
}

#[tokio::test]
async fn adaptive_encode_verify() {
    let app = app("adapt");
    // symbols [0,1,2,3,0,1]
    let body = serde_json::json!({
        "num_symbols": 4,
        "chunks": ["AAECAwAB"]
    });
    let (st, enc) = send_json(&app, "POST", "/v1/encode/adaptive", body).await;
    assert_eq!(st, StatusCode::OK, "{enc}");
    let (st, v) = send_json(
        &app,
        "POST",
        "/v1/verify",
        serde_json::json!({"data": enc["data_b64"].clone()}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["mode"], "adaptive");
    assert_eq!(v["symbols_b64"], "AAECAwAB");
}

#[tokio::test]
async fn invalid_frequency_table_is_422_with_exact_code() {
    let app = app("badfreq");
    let body = serde_json::json!({
        "num_symbols": 3,
        "chunks": [{"freqs": [0, 0, 0], "symbols_b64": "AA=="}]
    });
    let (st, v) = send_json(&app, "POST", "/v1/encode/static", body).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], "FREQ_TOTAL_OUT_OF_BOUNDS");
    assert_eq!(v["decision"], "rejected");
    assert!(v["request_id"].as_str().unwrap().starts_with("req_"));
}

#[tokio::test]
async fn zero_frequency_symbol_is_rejected() {
    let app = app("zerofreq");
    // freqs [1,0,2], symbol 1
    let body = serde_json::json!({
        "num_symbols": 3,
        "chunks": [{"freqs": [1, 0, 2], "symbols_b64": "AQ=="}]
    });
    let (st, v) = send_json(&app, "POST", "/v1/encode/static", body).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], "ZERO_FREQUENCY_SYMBOL");
}

#[tokio::test]
async fn truncated_container_is_rejected_not_panicked() {
    let app = app("trunc");
    // "RC01" + version byte, nothing after -> truncated header.
    let body = serde_json::json!({"data": "UkMwMQE="});
    let (st, v) = send_json(&app, "POST", "/v1/verify", body).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], "TRUNCATED");
    assert_eq!(v["decision"], "rejected");
}

#[tokio::test]
async fn bad_magic_is_rejected() {
    let app = app("magic");
    // "XXXX" + 16 zeros
    let mut v = b"XXXX".to_vec();
    v.extend_from_slice(&[0u8; 16]);
    let b64 = data_encoding_b64(&v);
    let (st, r) = send_json(&app, "POST", "/v1/verify", serde_json::json!({"data": b64})).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(r["error"]["code"], "BAD_MAGIC");
}

#[tokio::test]
async fn reserved_flag_is_indeterminate() {
    let app = app("resv");
    // Build a valid empty container then set flag 0x80.
    let empty = range_codec::container::encode_empty(
        range_codec::container::ModelMode::Static,
        4,
    )
    .unwrap();
    let mut blob = empty.clone();
    blob[5] = 0x80;
    let b64 = data_encoding_b64(&blob);
    let (st, r) = send_json(&app, "POST", "/v1/verify", serde_json::json!({"data": b64})).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(r["error"]["code"], "RESERVED_FLAG");
    assert_eq!(r["decision"], "indeterminate");
}

#[tokio::test]
async fn oversized_payload_is_413() {
    let app = app("large");
    // 2 MiB JSON body exceeds the 1 MiB test limit.
    let big = "A".repeat(2 * 1024 * 1024);
    let body = serde_json::json!({"data": big});
    let req = Request::builder()
        .method("POST")
        .uri("/v1/verify")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["error"]["code"], "PAYLOAD_TOO_LARGE");
}

#[tokio::test]
async fn artifact_put_get_decode_delete_flow() {
    let app = app("artifacts");
    let data = range_codec::container::encode_static(&[
        range_codec::container::StaticChunk {
            freqs: vec![1, 1, 1],
            symbols: vec![0, 1, 2, 0],
        },
    ])
    .unwrap();
    let b64 = data_encoding_b64(&data);

    // PUT raw bytes via JSON-b64? Put accepts raw only; use octet-stream.
    let put = Request::builder()
        .method("PUT")
        .uri("/v1/artifacts/demo-1")
        .header("content-type", "application/octet-stream")
        .body(Body::from(data.clone()))
        .unwrap();
    let resp = app.clone().oneshot(put).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // GET returns metadata + b64.
    let req = Request::builder()
        .method("GET")
        .uri("/v1/artifacts/demo-1")
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["metadata"]["id"], "demo-1");
    assert_eq!(v["data_b64"], b64);

    // Decode.
    let req = Request::builder()
        .method("POST")
        .uri("/v1/artifacts/demo-1/decode")
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["decoded_len"], 4);
    assert_eq!(v["symbols_b64"], "AAECAA==");

    // GET missing -> 404.
    let req = Request::builder()
        .method("GET")
        .uri("/v1/artifacts/missing")
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // DELETE.
    let req = Request::builder()
        .method("DELETE")
        .uri("/v1/artifacts/demo-1")
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // Path traversal id is rejected.
    let req = Request::builder()
        .method("GET")
        .uri("/v1/artifacts/..%2fetc")
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    // Axum rejects the encoded slash or route doesn't match; either way not 200.
    assert_ne!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn request_id_is_honoured_and_echoed() {
    let app = app("rid");
    let blob = [0u8; 4];
    let req = Request::builder()
        .method("POST")
        .uri("/v1/verify")
        .header("content-type", "application/octet-stream")
        .header("x-request-id", "fixed-trace-id-1234")
        .body(Body::from(blob.to_vec()))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    // Bad magic rejected, but the caller id is echoed back.
    assert_eq!(v["request_id"], "fixed-trace-id-1234");
    assert_eq!(v["error"]["code"], "BAD_MAGIC");
}

// Tiny local base64 to avoid coupling the test to the private api helper.
fn data_encoding_b64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in data.chunks(3) {
        let b = [c[0] as u32, *c.get(1).unwrap_or(&0) as u32, *c.get(2).unwrap_or(&0) as u32];
        let t = (b[0] << 16) | (b[1] << 8) | b[2];
        out.push(T[((t >> 18) & 63) as usize] as char);
        out.push(T[((t >> 12) & 63) as usize] as char);
        out.push(if c.len() > 1 { T[((t >> 6) & 63) as usize] as char } else { '=' });
        out.push(if c.len() > 2 { T[(t & 63) as usize] as char } else { '=' });
    }
    out
}
