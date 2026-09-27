//! HTTP API end-to-end tests (in-process, no network).

mod common;

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

use symex::api::build_router;
use symex::config::ConfigFile;

async fn post_json(uri: &str, body: Value) -> (StatusCode, Value) {
    let app = build_router(ConfigFile::default());
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), 1 << 24).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, v)
}

#[tokio::test]
async fn health_reports_versions() {
    let app = build_router(ConfigFile::default());
    let req = Request::builder()
        .uri("/v1/health")
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = to_bytes(resp.into_body(), 1 << 16).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
    assert_eq!(v["smt_backend"], "z3");
    assert!(v["smt_version"].as_str().unwrap().starts_with("4."));
    assert_eq!(v["version"], env!("CARGO_PKG_VERSION"));
}

#[tokio::test]
async fn analyze_unsafe_program_over_http() {
    let (status, v) = post_json(
        "/v1/analyze",
        json!({
            "source": "param x: u8; let y: u8 = x + 1u8; assert(y != 0u8);"
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["verdict"], "unsafe");
    assert_eq!(v["findings"][0]["counterexample"]["x"], 255);
    assert_eq!(v["findings"][0]["replay"]["status"], "reproduced");
    assert!(v["run_id"].as_str().unwrap().starts_with("run-"));
    assert_eq!(v["smt_backend"], "z3");
    assert!(v["engine"]["max_paths"].is_number());
    assert!(v["budget"]["paths_explored"].is_number());
}

#[tokio::test]
async fn analyze_safe_program_over_http() {
    let (status, v) = post_json(
        "/v1/analyze",
        json!({"source": "param x: u8; let y: u8 = x & 7u8; assert(y < 8u8);"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["verdict"], "safe");
    assert_eq!(v["findings"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn unknown_is_not_reported_as_safe() {
    // Loop beyond the unrolling bound: verdict must be unknown.
    let (status, v) = post_json(
        "/v1/analyze",
        json!({
            "source": "param x: u8; let i: u8 = 0u8; while (i < x) { i = i + 1u8; } assert(i >= 0u8);",
            "engine": {"loop_unroll": 2}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["verdict"], "unknown");
    assert_ne!(v["verdict"], "safe");
    assert!(v["path_counts"]["incomplete_unroll"].as_u64().unwrap() >= 1);
}

#[tokio::test]
async fn invalid_program_is_400_with_stable_code() {
    let (status, v) = post_json(
        "/v1/analyze",
        json!({"source": "param x: u8; assert(y > 0u8);"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], "invalid_program");
    assert!(v["error"]["message"].as_str().unwrap().contains("undeclared"));
}

#[tokio::test]
async fn conflicting_submission_is_400() {
    let (status, v) = post_json(
        "/v1/analyze",
        json!({
            "source": "param x: u8;",
            "json": {"params": ["x: u8"], "body": []}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], "invalid_request");
}

#[tokio::test]
async fn json_envelope_submission() {
    let (status, v) = post_json(
        "/v1/analyze",
        json!({
            "json": {
                "params": ["x: u8"],
                "body": ["let y: u8 = x + 1u8;", "assert(y != 0u8);"]
            }
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["verdict"], "unsafe");
}

#[tokio::test]
async fn replay_endpoint_runs_concrete_interpreter() {
    let (status, v) = post_json(
        "/v1/replay",
        json!({
            "source": "param x: u8; let y: u8 = x + 1u8; assert(y != 0u8);",
            "input": {"x": 255}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["outcome"], "failed");
    assert_eq!(v["failure"]["kind"], "assertion_failed");

    let (status, v) = post_json(
        "/v1/replay",
        json!({
            "source": "param x: u8; let y: u8 = x + 1u8; assert(y != 0u8);",
            "input": {"x": 12}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["outcome"], "completed");

    // Out-of-range input is a 400, not a crash.
    let (status, v) = post_json(
        "/v1/replay",
        json!({
            "source": "param x: u8; assert(x == x);",
            "input": {"x": 999}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], "invalid_input");
}
