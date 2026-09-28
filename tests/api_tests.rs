//! HTTP API tests (in-process, no socket) plus run-log diagnostics checks.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;
use wtio::api::app;

async fn post_json(uri: &str, payload: serde_json::Value) -> (StatusCode, serde_json::Value) {
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&payload).unwrap()))
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let json = serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null);
    (status, json)
}

fn fixture(name: &str) -> serde_json::Value {
    let bytes = std::fs::read(format!("fixtures/{name}.json")).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn health_ok() {
    let resp = app()
        .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
}

#[tokio::test]
async fn check_included() {
    let (status, body) = post_json("/api/v1/check", fixture("hidden_internal_steps_included")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["verdict"], "included");
    assert_eq!(body["aligned_alphabet"], serde_json::json!(["a"]));
    assert!(body["run_id"].as_str().unwrap().starts_with("run-"));
}

#[tokio::test]
async fn check_not_included_carries_verified_evidence() {
    let (status, body) = post_json("/api/v1/check", fixture("erroneous_extra_output")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["verdict"], "not_included");
    assert_eq!(body["counterexample"]["trace"], serde_json::json!(["x"]));
    assert_eq!(body["verification"]["confirmed"], true);
    assert_eq!(
        body["counterexample"]["implementation_replay"]["hops"][0]["before_tau"][0]["action"],
        "tau"
    );
}

#[tokio::test]
async fn check_unknown_is_200_with_verdict_unknown() {
    // In-search resource exhaustion is a successful response carrying an
    // unknown verdict, not an HTTP error.
    let (status, body) = post_json("/api/v1/check", fixture("resource_exhausted_unknown")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["verdict"], "unknown");
    assert_eq!(body["unknown"]["reason"], "pair_limit");
    assert!(body["counterexample"].is_null());
}

#[tokio::test]
async fn malformed_json_is_400_input_error() {
    let req = Request::builder()
        .method("POST")
        .uri("/api/v1/check")
        .body(Body::from("{ not json"))
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["error"]["kind"], "input_error");
    assert_eq!(v["error"]["code"], "invalid_json");
}

#[tokio::test]
async fn unknown_state_is_409_state_conflict() {
    let mut f = fixture("hidden_internal_steps_included");
    f["implementation"]["initial"] = "ghost".into();
    let (status, body) = post_json("/api/v1/check", f).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["kind"], "state_conflict");
    assert_eq!(body["error"]["code"], "unknown_state");
}

#[tokio::test]
async fn oversized_request_is_413() {
    let mut f = fixture("hidden_internal_steps_included");
    f["limits"] = serde_json::json!({"max_states_per_lts": 1});
    let (status, body) = post_json("/api/v1/check", f).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(body["error"]["kind"], "resource_exhausted");
    assert_eq!(body["error"]["code"], "too_many_states");
}

#[tokio::test]
async fn unknown_route_is_404() {
    let resp = app()
        .oneshot(Request::builder().uri("/nope").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn verify_replay_endpoint_roundtrip() {
    // First get a real counterexample + replay, then submit it to the
    // independent verify endpoint.
    let f = fixture("erroneous_extra_output");
    let (_, check_body) = post_json("/api/v1/check", f.clone()).await;
    let payload = serde_json::json!({
        "model": f,
        "trace": check_body["counterexample"]["trace"],
        "replay": check_body["counterexample"]["implementation_replay"],
    });
    let (status, body) = post_json("/api/v1/verify-replay", payload).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["confirmed"], true);
}

#[test]
fn run_log_is_written_and_replayable() {
    let dir = std::env::temp_dir().join(format!("wtio-test-logs-{}", std::process::id()));
    std::env::set_var("WTIO_LOG_DIR", &dir);
    let req: wtio::input::CheckRequest =
        serde_json::from_value(fixture("erroneous_extra_output")).unwrap();
    let resp = wtio::engine::run_check(&req).unwrap();

    let path = dir.join(format!("{}.jsonl", resp.run_id));
    let content = std::fs::read_to_string(&path).expect("log file must exist");
    let lines: Vec<serde_json::Value> = content
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let kinds: Vec<&str> = lines.iter().map(|l| l["kind"].as_str().unwrap()).collect();
    assert_eq!(kinds[0], "request_received");
    assert!(kinds.contains(&"compiled"));
    assert!(kinds.contains(&"solver_done"));
    assert!(kinds.contains(&"counterexample_verified"));
    // The verification event keeps the decisive evidence for replay.
    let verified = lines
        .iter()
        .find(|l| l["kind"] == "counterexample_verified")
        .unwrap();
    assert_eq!(verified["detail"]["trace"], serde_json::json!(["x"]));
    assert_eq!(verified["detail"]["confirmed"], true);
    assert!(verified["detail"]["impl_macro_path"].is_array());

    std::env::remove_var("WTIO_LOG_DIR");
    let _ = std::fs::remove_dir_all(&dir);
}
