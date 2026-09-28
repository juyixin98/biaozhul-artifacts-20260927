//! HTTP end-to-end tests against the in-process Axum router.
//! These exercise the real request/response JSON and request-id
//! correlation, asserting concrete verdicts and failure categories.

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use fsm_server::app::app;
use fsm_server::config::BudgetConfig;
use tower::util::ServiceExt;

fn fixture_json(name: &str) -> serde_json::Value {
    let path = format!("{}/../../fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    let text = std::fs::read_to_string(path).unwrap();
    serde_json::from_str(&text).unwrap()
}

async fn post_json(uri: &str, body: serde_json::Value, req_id: Option<&str>) -> (StatusCode, axum::http::HeaderMap, serde_json::Value) {
    let mut builder = Request::builder().method("POST").uri(uri);
    if let Some(id) = req_id {
        builder = builder.header("x-request-id", id);
    }
    let request = builder
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let response = app(BudgetConfig::default())
        .oneshot(request)
        .await
        .unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or(serde_json::json!({}));
    (status, headers, json)
}

async fn get(uri: &str) -> (StatusCode, serde_json::Value) {
    let request = Request::builder().uri(uri).body(Body::empty()).unwrap();
    let response = app(BudgetConfig::default()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    (status, json)
}

fn check_body(spec: serde_json::Value) -> serde_json::Value {
    serde_json::json!({ "spec": spec })
}

#[tokio::test]
async fn health_and_version() {
    let (s, h) = get("/health").await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(h["status"], "ok");

    let (s, v) = get("/version").await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["engine"], "local-fsm-checker");
    assert!(v["version"].is_string());
    assert_eq!(v["logics"][0], "AG safety invariants");
}

#[tokio::test]
async fn mutex_check_returns_counterexample_and_replay_valid() {
    let (status, headers, json) = post_json(
        "/check",
        check_body(fixture_json("mutex_bad")),
        Some("req-mutex-001"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-request-id").unwrap().to_str().unwrap(),
        "req-mutex-001"
    );
    assert_eq!(json["request_id"], "req-mutex-001");
    assert_eq!(json["status"], "complete");
    assert_eq!(json["truncated"], false);
    assert!(json["spec_fingerprint"].as_str().unwrap().len() == 64);
    assert_eq!(json["properties"][0]["name"], "mutex");
    assert_eq!(json["properties"][0]["verdict"], "false");
    assert_eq!(json["properties"][0]["reason"], "COUNTEREXAMPLE_FOUND");
    assert_eq!(
        json["properties"][0]["evidence"]["trace"].as_array().unwrap().len(),
        5
    );
    // Server independently replayed the trace it returned.
    assert_eq!(json["evidence_checks"][0]["replay_valid"], true);
}

#[tokio::test]
async fn missing_request_id_is_generated_locally() {
    let (status, headers, json) =
        post_json("/check", check_body(fixture_json("counter")), None).await;
    assert_eq!(status, StatusCode::OK);
    let generated = headers.get("x-request-id").unwrap().to_str().unwrap();
    assert!(generated.starts_with("local-"));
    assert_eq!(json["request_id"], generated);
    assert_eq!(json["status"], "complete");

    let props = json["properties"].as_array().unwrap();
    let by = |n: &str| {
        props
            .iter()
            .find(|p| p["name"] == n)
            .unwrap()
            .clone()
    };
    assert_eq!(by("nonnegative")["verdict"], "true");
    assert_eq!(by("can_reach_top")["verdict"], "true");
    assert_eq!(by("can_overflow")["verdict"], "false");
    assert_eq!(by("can_overflow")["reason"], "TARGET_UNREACHABLE");
}

#[tokio::test]
async fn no_initial_state_returns_explicit_failure_category() {
    let (status, _, json) =
        post_json("/check", check_body(fixture_json("no_initial")), Some("r2")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["status"], "invalid");
    assert_eq!(json["reason"], "NO_INITIAL_STATE");
    assert_eq!(json["stats"]["initial_states"], 0);
    assert!(json["properties"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn truncated_run_reports_unknown_not_refuted() {
    let mut body = check_body(fixture_json("truncate_chain"));
    body["budget"] = serde_json::json!({ "max_states": 4 });
    let (status, _, json) = post_json("/check", body, Some("r3")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["status"], "truncated");
    assert_eq!(json["truncated"], true);
    assert_eq!(json["reason"], "BUDGET_TRUNCATED");
    assert_eq!(json["stats"]["explored"], 4);
    for p in json["properties"].as_array().unwrap() {
        assert_eq!(p["verdict"], "unknown");
        assert!(p["evidence"].is_null());
    }
}

#[tokio::test]
async fn malformed_spec_returns_400_with_category() {
    let (status, _, json) = post_json(
        "/check",
        serde_json::json!({ "spec": { "name": "broken" } }),
        Some("r4"),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(json["error"]["code"], "INVALID_SPEC");
}

#[tokio::test]
async fn malformed_json_returns_400() {
    let request = Request::builder()
        .method("POST")
        .uri("/check")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from("{ not json"))
        .unwrap();
    let response = app(BudgetConfig::default()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn out_of_domain_is_separate_failure_block() {
    let (status, _, json) =
        post_json("/check", check_body(fixture_json("out_of_domain")), Some("r5")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["status"], "error");
    assert_eq!(json["failure"]["code"], "VALUE_OUT_OF_DOMAIN");
    assert_eq!(json["failure"]["transition"], "jump");
}

#[tokio::test]
async fn evidence_replay_endpoint_detects_tampering() {
    // First obtain a real counterexample.
    let (_, _, check) =
        post_json("/check", check_body(fixture_json("mutex_bad")), Some("r6")).await;
    let mut evidence = check["properties"][0]["evidence"].clone();
    // Tamper: drop the root's assignment.
    evidence["trace"][0]["state"] = serde_json::json!([]);
    let body = serde_json::json!({
        "spec": fixture_json("mutex_bad"),
        "evidence": evidence,
    });
    let (status, _, json) = post_json("/evidence/replay", body, Some("r6-verify")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["request_id"], "r6-verify");
    assert_eq!(json["report"]["valid"], false);
    assert_eq!(json["report"]["failure"]["code"], "BAD_STATE");
}

#[tokio::test]
async fn evidence_replay_endpoint_accepts_valid_trace() {
    let (_, _, check) =
        post_json("/check", check_body(fixture_json("mutex_bad")), Some("r7")).await;
    let body = serde_json::json!({
        "spec": fixture_json("mutex_bad"),
        "evidence": check["properties"][0]["evidence"].clone(),
    });
    let (status, _, json) = post_json("/evidence/replay", body, Some("r7-verify")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["report"]["valid"], true);
    assert_eq!(json["report"]["steps"].as_array().unwrap().len(), 5);
}
