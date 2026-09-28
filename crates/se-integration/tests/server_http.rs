//! End-to-end HTTP tests driving the Axum router directly with `oneshot` — no open
//! network ports, no HTTP client dependency.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use body_util::BodyExt;
use tower::ServiceExt;
use se_integration::common::{ASSERT_FAIL, INFEASIBLE_BRANCH, WRAP_AROUND};
use se_server::api::AppState;
use se_server::app;
use se_server::config::AppConfig;

fn router() -> Router {
    app(AppState::new(AppConfig::default()), 1 << 20)
}

async fn send(
    router: &Router,
    method: &str,
    path: &str,
    content_type: Option<&str>,
    body: Option<serde_json::Value>,
) -> (StatusCode, serde_json::Value) {
    let builder = Request::builder().method(method).uri(path);
    let builder = if let Some(ct) = content_type {
        builder.header("content-type", ct)
    } else {
        builder
    };
    let body = match body {
        Some(v) => Body::from(v.to_string()),
        None => Body::empty(),
    };
    let resp = router.clone().oneshot(builder.body(body).unwrap()).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let parsed = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| serde_json::json!({"raw": String::from_utf8_lossy(&bytes)}));
    (status, parsed)
}

async fn post(
    router: &Router,
    path: &str,
    body: serde_json::Value,
) -> (StatusCode, serde_json::Value) {
    send(router, "POST", path, Some("application/json"), Some(body)).await
}

#[tokio::test]
async fn health_and_version_report_backend() {
    let r = router();
    let (s, h) = send(&r, "GET", "/health", None, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(h["status"], "ok");
    assert_eq!(h["solver"], "z3-cli");
    assert!(h["solver_available"].as_bool().unwrap());

    let (s, v) = send(&r, "GET", "/version", None, None).await;
    assert_eq!(s, StatusCode::OK);
    assert!(v["engine"].as_str().unwrap().starts_with("se-engine"));
    assert_eq!(v["solver_interface"], "SMT-LIB 2 / QF_BV");
}

#[tokio::test]
async fn analyze_returns_confirmed_counterexample_and_ids() {
    let r = router();
    let program: serde_json::Value = serde_json::from_str(ASSERT_FAIL).unwrap();
    let (status, v) = post(
        &r,
        "/analyze",
        serde_json::json!({"request_id":"rid-http-1","with_oracle":true,"program":program}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["request_id"], "rid-http-1");
    assert!(v["run_id"].as_str().unwrap().starts_with("r-"));
    assert!(v["program_id"].as_str().unwrap().starts_with("p-"));
    assert_eq!(v["verdict"], "violation");
    assert_eq!(v["replay"]["confirmed_count"], 1);
    assert_eq!(v["replay"]["rejected_count"], 0);
    assert_eq!(v["replay"]["verified"][0]["status"], "confirmed");
    assert_eq!(v["replay"]["verified"][0]["replay_stmt"], 0);
    assert_eq!(v["oracle"]["verdict"], "violation");
    assert_eq!(v["oracle"]["total_assignments"], 256);
    assert_eq!(v["report"]["budget"]["max_loop_unroll"], 64);
    assert_eq!(v["report"]["budget"]["max_paths"], 256);
}

#[tokio::test]
async fn analyze_holds_with_oracle_on_infeasible_branch() {
    let r = router();
    let program: serde_json::Value = serde_json::from_str(INFEASIBLE_BRANCH).unwrap();
    let (status, v) = post(
        &r,
        "/analyze",
        serde_json::json!({"with_oracle":true,"program":program}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["verdict"], "holds");
    assert_eq!(v["oracle"]["verdict"], "holds");
    assert_eq!(v["oracle"]["completed"], 3);
    assert_eq!(v["oracle"]["infeasible_assume"], 253);
}

#[tokio::test]
async fn analyze_wrap_scenario_matches_oracle_failing_set() {
    let r = router();
    let program: serde_json::Value = serde_json::from_str(WRAP_AROUND).unwrap();
    let (_, v) = post(
        &r,
        "/analyze",
        serde_json::json!({"with_oracle":true,"program":program}),
    )
    .await;
    assert_eq!(v["verdict"], "violation");
    let xs: std::collections::BTreeSet<u64> = v["oracle"]["failures"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["inputs"]["x"].as_u64().unwrap())
        .collect();
    let expected: std::collections::BTreeSet<u64> =
        (0..=100u64).chain(156..=255u64).collect();
    assert_eq!(xs, expected);
}

#[tokio::test]
async fn replay_endpoint_classifies_concrete_runs() {
    let r = router();
    let program: serde_json::Value = serde_json::from_str(ASSERT_FAIL).unwrap();

    let (s1, ok) = post(
        &r,
        "/verify/replay",
        serde_json::json!({"program":program,"inputs":{"x":3}}),
    )
    .await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(ok["outcome"], "completed");

    let program2: serde_json::Value = serde_json::from_str(ASSERT_FAIL).unwrap();
    let (s2, bad) = post(
        &r,
        "/verify/replay",
        serde_json::json!({"program":program2,"inputs":{"x":42}}),
    )
    .await;
    assert_eq!(s2, StatusCode::OK);
    assert_eq!(bad["outcome"], "failed");
    assert_eq!(bad["failure_kind"], "assertion");
    assert_eq!(bad["failure_stmt"], 0);
    assert!(bad["trace"]
        .as_array()
        .unwrap()
        .contains(&serde_json::json!(0)));
}

#[tokio::test]
async fn malformed_requests_are_structured_errors_not_success() {
    let r = router();

    // Bad width -> 400 structured body.
    let (status, v) = post(
        &r,
        "/oracle",
        serde_json::json!({"program":{"width":7,"body":[]}}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"], "bad_request");
    assert!(v["message"].as_str().unwrap().contains("width"));

    // Missing required field -> 400 (mapped from 422).
    let (status, v) = post(&r, "/analyze", serde_json::json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"], "bad_request");

    // Wrong content type rejected at middleware.
    let req = Request::builder()
        .method("POST")
        .uri("/analyze")
        .header("content-type", "text/plain")
        .body(Body::from("{}"))
        .unwrap();
    let resp = r.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::UNSUPPORTED_MEDIA_TYPE);
}

#[tokio::test]
async fn oversized_body_is_rejected() {
    // Build a router with a deliberately small limit.
    let r = app(AppState::new(AppConfig::default()), 1024);
    let big = "x".repeat(4096);
    let (status, v) = post(
        &r,
        "/analyze",
        serde_json::json!({"program":{"width":8,"note":big,"body":[]}}),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(v["error"], "payload_too_large");
}
