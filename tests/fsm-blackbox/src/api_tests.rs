use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use fsm_api::{app, Config, ServiceState};
use tower::ServiceExt;

fn router() -> axum::Router {
    app(Arc::new(ServiceState {
        config: Config::default(),
    }))
}

async fn body_json(resp: axum::response::Response) -> serde_json::Value {
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn health_and_version() {
    let resp = router()
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;
    assert_eq!(j["status"], "ok");

    let resp = router()
        .oneshot(
            Request::builder()
                .uri("/api/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let j = body_json(resp).await;
    assert_eq!(j["service"], "explicit-fsm-checker");
    assert!(j["versions"]["fsm-core"].is_string());
}

#[tokio::test]
async fn check_mutex_bad_returns_witness_and_request_id() {
    let payload = serde_json::json!({
        "fixture": "mutex_bad",
        "properties": [{"name": "mutex", "kind": "ag", "expr": "!(in1 && in2)"}],
        "check_deadlock": true
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .header("x-request-id", "test-rid-42")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        resp.headers().get("x-request-id").unwrap(),
        "test-rid-42",
        "inbound request id must be correlated"
    );
    let j = body_json(resp).await;
    assert_eq!(j["request_id"], "test-rid-42");
    assert_eq!(j["processing"]["location"], "fsm-core::run_check");
    let mutex = j["properties"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["name"] == "mutex")
        .unwrap();
    assert_eq!(mutex["conclusion"], "violated");
    assert_eq!(mutex["evidence"]["length"], 2);
}

#[tokio::test]
async fn truncated_run_marks_unknown_separately() {
    let payload = serde_json::json!({
        "fixture": "big_counter",
        "properties": [{"name": "ag", "kind": "ag", "expr": "x <= 5000"}],
        "check_deadlock": false,
        "max_states": 100
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;
    assert_eq!(j["status"], "truncated");
    assert_eq!(j["properties"][0]["conclusion"], "unknown");
    assert!(j["properties"][0]["reason"]
        .as_str()
        .unwrap()
        .contains("NOT proven"));
    assert!(j["error"].is_null());
}

#[tokio::test]
async fn no_init_returns_classified_error() {
    let payload = serde_json::json!({
        "fixture": "no_init",
        "properties": []
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "run errors are 200 with status=error"
    );
    let j = body_json(resp).await;
    assert_eq!(j["status"], "error");
    assert_eq!(j["error"]["kind"], "no_initial_state");
}

#[tokio::test]
async fn unknown_fixture_and_bad_expr_are_400_with_categories() {
    let payload = serde_json::json!({"fixture": "nope", "properties": []});
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let j = body_json(resp).await;
    assert_eq!(j["error"]["category"], "unknown_fixture");

    let payload = serde_json::json!({
        "fixture": "counter",
        "properties": [{"name": "p", "kind": "ag", "expr": "nope + 1"}]
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let j = body_json(resp).await;
    assert_eq!(j["error"]["category"], "unknown_name");
}

#[tokio::test]
async fn json_spec_roundtrip_counter() {
    let payload = serde_json::json!({
        "spec_json": fsm_fixtures::counter_json(),
        "properties": [{"name": "cap", "kind": "ef", "expr": "x == 3"}],
        "check_deadlock": false
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;
    assert_eq!(j["properties"][0]["conclusion"], "violated");
    assert_eq!(j["stats"]["states_consumed"], 4);
}

#[tokio::test]
async fn verify_endpoint_accepts_real_witness() {
    // first get a witness
    let payload = serde_json::json!({
        "fixture": "mutex_bad",
        "properties": [{"name": "mutex", "kind": "ag", "expr": "!(in1 && in2)"}],
        "check_deadlock": false
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/check")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let j = body_json(resp).await;
    let evidence = j["properties"][0]["evidence"].clone();

    let verify_payload = serde_json::json!({
        "fixture": "mutex_bad",
        "evidence": {
            "kind": evidence["kind"],
            "expr": "!(in1 && in2)",
            "length": evidence["length"],
            "path": evidence["path"]
        }
    });
    let resp = router()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/verify")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&verify_payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;
    assert_eq!(j["ok"], true);
    assert_eq!(j["report"]["accepted"], true);
}
