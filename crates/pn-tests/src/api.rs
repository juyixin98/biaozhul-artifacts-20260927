//! End-to-end HTTP tests that boot the real Axum router in-process.
//!
//! These assert concrete verdicts, concrete HTTP status/failure categories and
//! independent witness verification in the response body - not merely that the
//! endpoint answers.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use pn_server::config::Config;
use pn_server::http::{router, AppState};
use tower::ServiceExt;

use pn_fixtures::nets;

fn app() -> axum::Router {
    let cfg = Config {
        http_bind: "test".into(),
        ..Config::default()
    };
    router(AppState {
        config: Arc::new(cfg),
    })
}

async fn post_json(body: String, run_hint: Option<&str>) -> (StatusCode, serde_json::Value) {
    let mut req = Request::builder()
        .method("POST")
        .uri("/api/v1/analyze")
        .header("content-type", "application/json");
    if let Some(h) = run_hint {
        req = req.header("x-run-id", h);
    }
    let req = req.body(Body::from(body)).unwrap();
    let resp = app().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("response is JSON");
    (status, value)
}

#[tokio::test]
async fn health_and_version_report_bounded_scope() {
    let resp = app()
        .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let resp = app()
        .oneshot(Request::builder().uri("/version").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["schema"], "petri-analysis/v1");
    // The service must not claim to decide unbounded reachability.
    assert_eq!(v["unbounded_reachability_complete"], false);
}

#[tokio::test]
async fn mutex_request_returns_concrete_verdicts_and_verified_witness() {
    let (status, v) = post_json(nets::mutex_request_json(), Some("api-mutex-1")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["run_id"], "api-mutex-1");
    let targets = v["result"]["targets"].as_array().unwrap();

    // Target 0 "held" (0,1): reachable in one acquire, witness verified.
    let held = &targets[0];
    assert_eq!(held["verdict"], "REACHABLE");
    assert_eq!(held["reachable"], true);
    assert_eq!(held["distance"], 1);
    assert_eq!(held["path"][0]["transition"], "acquire");
    assert_eq!(held["path"][0]["after"], serde_json::json!([0, 1]));
    assert_eq!(held["witness_verification"]["valid"], true);

    // Target 1 (1,1): unreachable.
    let impossible = &targets[1];
    assert_eq!(impossible["verdict"], "UNREACHABLE");
    assert_eq!(impossible["reachable"], false);
    assert!(impossible["path"].is_null());

    // No deadlocks; scope disclaimer present and honest.
    assert!(v["result"]["deadlocks"].as_array().unwrap().is_empty());
    assert_eq!(
        v["result"]["scope"]["equivalent_to_unbounded_reachability_decision"],
        false
    );
}

#[tokio::test]
async fn producer_consumer_request_marks_capacity_deadlock() {
    let (status, v) = post_json(nets::producer_consumer_request_json(), None).await;
    assert_eq!(status, StatusCode::OK);
    let deadlocks = v["result"]["deadlocks"].as_array().unwrap();
    assert_eq!(deadlocks.len(), 1);
    assert_eq!(deadlocks[0]["marking"], serde_json::json!([0, 2, 2]));
    // The deadlock claim is independently verified in the response.
    assert_eq!(deadlocks[0]["independent_verification"]["is_deadlock"], true);

    let targets = v["result"]["targets"].as_array().unwrap();
    assert_eq!(targets[0]["verdict"], "REACHABLE"); // one-consumed
    assert_eq!(targets[1]["verdict"], "UNREACHABLE"); // token-vanished
    assert_eq!(targets[2]["verdict"], "REACHABLE"); // buffer-full
}

#[tokio::test]
async fn malformed_json_is_400_syntax() {
    let (status, v) = post_json("{not json".into(), Some("bad-syntax")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["status"], "error");
    assert_eq!(v["error"]["category"], "SYNTAX");
    assert_eq!(v["run_id"], "bad-syntax");
}

#[tokio::test]
async fn schema_violation_is_400_schema() {
    let body = serde_json::json!({
        "schema": "petri-analysis/v1",
        "places": [{"name": "p", "capacity": 1}]
        // transitions and initial intentionally missing
    })
    .to_string();
    let (status, v) = post_json(body, None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["category"], "SCHEMA");
    assert_eq!(v["error"]["details"][0]["pointer"], "/transitions");
}

#[tokio::test]
async fn unknown_place_reference_is_422_semantic() {
    let body = serde_json::json!({
        "schema": "petri-analysis/v1",
        "places": [{"name": "p", "capacity": 1}],
        "transitions": [{"name": "t", "inputs": [{"place": "ghost", "weight": 1}], "outputs": []}],
        "initial": [0]
    })
    .to_string();
    let (status, v) = post_json(body, None).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["category"], "SEMANTIC");
    assert!(v["error"]["details"][0]["pointer"]
        .as_str()
        .unwrap()
        .contains("inputs"));
}

#[tokio::test]
async fn target_over_capacity_is_rejected_not_clamped() {
    // The service must not silently clamp an out-of-capacity target to fit.
    let body = serde_json::json!({
        "schema": "petri-analysis/v1",
        "places": [{"name": "p", "capacity": 1}],
        "transitions": [],
        "initial": [0],
        "targets": [{"marking": [2]}]
    })
    .to_string();
    let (status, v) = post_json(body, None).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["category"], "SEMANTIC");
}

#[tokio::test]
async fn unknown_run_hint_is_replaced_with_generated_id() {
    let (_, v) = post_json(nets::mutex_request_json(), Some("has spaces invalid")).await;
    assert!(v["run_id"].as_str().unwrap().starts_with("run-"));
}
