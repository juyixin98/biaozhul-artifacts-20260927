//! End-to-end tests through the Axum router using in-process requests
//! (tower::ServiceExt::oneshot) — no open network port required.
#[path = "common/mod.rs"]
mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use common::fixture;
use ia_app::http::router;
use serde_json::{json, Value};
use tower::ServiceExt;

async fn post(path: &str, body: Value) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = router().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let json: Value = serde_json::from_slice(&bytes).unwrap();
    (status, json)
}

#[tokio::test]
async fn analyze_endpoint_correlates_request_and_separates_uncertainty() {
    let src = fixture("03_overflow_paths.ial");
    let (status, json) = post(
        "/api/analyze",
        json!({ "source": src, "request_id": "rid-abc-123" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    // Request identity is echoed; versions are present.
    assert_eq!(json["request_id"], "rid-abc-123");
    assert!(json["solver_version"].is_string());
    assert!(json["lang_version"].is_string());
    assert_eq!(json["ok"], true);
    // Steps show the processing locations.
    let stages: Vec<&str> = json["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["stage"].as_str().unwrap())
        .collect();
    assert_eq!(stages, vec!["parse", "resolve", "analyze"]);
    // Uncertain (possible) and guaranteed findings both present, distinct.
    let counts = &json["data"]["counts"];
    assert!(counts["possible_failure"].as_u64().unwrap() >= 1);
    assert_eq!(counts["guaranteed_failure"].as_u64().unwrap(), 1);
    let possible = json["data"]["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["verdict"] == "possible_failure")
        .unwrap();
    // Over-approximation wording, never "this is a bug".
    let expl = possible["explanation"].as_str().unwrap();
    assert!(expl.contains("over-approximation"));
    assert!(possible["certainty"] == "possible");
}

#[tokio::test]
async fn verify_endpoint_runs_exhaustive_and_reports_sound() {
    let src = fixture("06_countdown.ial");
    let (status, json) = post(
        "/api/verify",
        json!({ "source": src, "request_id": "rid-verify-1" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["ok"], true);
    let data = &json["data"];
    assert_eq!(data["sound"], true);
    assert_eq!(data["enumeration_complete"], true);
    assert_eq!(data["combinations_run"], 7); // start in [0, 6]
    assert!(data["violations"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn parse_error_is_a_diagnostic_not_a_500() {
    let (status, json) = post(
        "/api/analyze",
        json!({ "source": "input x [0: 1]; { x = ; }\n", "request_id": "rid-bad" }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json["ok"], false);
    assert!(!json["diagnostics"].as_array().unwrap().is_empty());
    let d = &json["diagnostics"][0];
    assert!(d["start_line"].as_u64().unwrap() >= 1);
    assert!(d["source_excerpt"].as_str().unwrap().contains('^'));
}

#[tokio::test]
async fn enumeration_cap_exceeded_is_reported_as_not_run() {
    // Domain of 2000 combinations; cap of 100 => exhaustive check NOT run.
    let src = "input x [1: 2000]; { skip; }\n";
    let (status, json) = post(
        "/api/verify",
        json!({ "source": src, "request_id": "rid-cap", "enumeration_cap": 100 }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json["ok"], false);
    assert!(json["diagnostics"][0]["message"]
        .as_str()
        .unwrap()
        .contains("NOT run"));
}

#[tokio::test]
async fn healthz_reports_versions() {
    let req = Request::builder()
        .method("GET")
        .uri("/healthz")
        .body(Body::empty())
        .unwrap();
    let resp = router().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let json: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(json["status"], "ok");
    assert!(json["solver_version"].is_string());
}
