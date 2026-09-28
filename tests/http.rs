//! End-to-end HTTP tests against the real Axum router (no network socket:
//! requests are driven through `oneshot`). These assert concrete statuses,
//! error codes/categories, run-id propagation, batch atomicity and
//! restore-then-continue equivalence.

mod common;

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use btmon::api::router;
use btmon::store::Store;
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;

fn app() -> axum::Router {
    router(Arc::new(Store::new()))
}

async fn send(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
    run_id: Option<&str>,
) -> (StatusCode, Value, String) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(rid) = run_id {
        builder = builder.header("x-run-id", rid);
    }
    let req = match body {
        Some(v) => builder
            .header("content-type", "application/json")
            .body(Body::from(serde_json::to_vec(&v).unwrap()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let rid = resp
        .headers()
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value, rid)
}

const RULESET: &str = r#"{
  "version": "v1",
  "response": [{
    "id": "pay",
    "trigger": { "all": [ { "field": "kind", "op": "eq", "value": "order" } ] },
    "response": { "field": "kind", "op": "eq", "value": "payment" },
    "after": 0, "within": 3
  }]
}"#;

#[tokio::test]
async fn health_ok() {
    let (s, v, _) = send(&app(), "GET", "/healthz", None, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["status"], json!("ok"));
}

#[tokio::test]
async fn create_get_append_end_full_flow() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let (s, v, rid) = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "m1", "ruleset": ruleset})),
        Some("http-run-1"),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(rid, "http-run-1");
    assert_eq!(v["run_id"], json!("http-run-1"));
    assert_eq!(v["data"]["status"]["global_verdict"], json!("sat")); // vacuous pre-trace

    // Pending after an order -> wait.
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors/m1/events",
        Some(json!({"kind": "order"})),
        Some("http-run-1"),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["results"][0]["verdict"], json!("wait"));
    assert_eq!(v["data"]["results"][0]["spawned"][0], json!("0:pay:0"));

    let (s, v, _) = send(&app, "GET", "/monitors/m1", None, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["global_verdict"], json!("wait"));
    assert_eq!(v["data"]["obligations_pending"], 1);

    // Payment within window -> satisfied.
    let (s, _, _) = send(
        &app,
        "POST",
        "/monitors/m1/events",
        Some(json!({"kind": "payment"})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let (_, v, _) = send(&app, "GET", "/monitors/m1/obligations", None, None).await;
    assert_eq!(v["data"]["obligations"][0]["status"], json!("satisfied"));
    assert_eq!(v["data"]["obligations"][0]["reason"], json!("fulfilled"));

    let (s, v, _) = send(&app, "POST", "/monitors/m1/end", None, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["verdict"], json!("sat"));
}

#[tokio::test]
async fn missing_response_violates_on_end_but_waits_before() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let (s, _, _) = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"ruleset": ruleset})),
        Some("r2"),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let id = {
        // find created id via list (we used generated id)
        let (_, l, _) = send(&app, "GET", "/monitors", None, None).await;
        l["data"]["monitors"][0].as_str().unwrap().to_string()
    };
    let (s, v, _) = send(
        &app,
        "POST",
        &format!("/monitors/{id}/events"),
        Some(json!({"kind": "order"})),
        Some("r2"),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["results"][0]["verdict"], json!("wait"));
    let (s, v, _) = send(
        &app,
        "POST",
        &format!("/monitors/{id}/end"),
        None,
        Some("r2"),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["verdict"], json!("viol"));
    assert_eq!(v["run_id"], json!("r2"));
}

#[tokio::test]
async fn error_bodies_are_classified() {
    let app = app();
    // input: malformed JSON
    let req = Request::post("/monitors")
        .header("content-type", "application/json")
        .body(Body::from("{not json"))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["error"]["code"], json!("MALFORMED_JSON"));
    assert_eq!(v["error"]["category"], json!("input"));

    // not found
    let (s, v, _) = send(&app, "GET", "/monitors/nope", None, None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(v["error"]["code"], json!("NOT_FOUND"));

    // input: invalid ruleset -> 400 BAD_WINDOW
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"ruleset": {"version": "v", "response": [
            {"id": "r", "trigger": {"all": []}, "response": {"field": "k","op":"eq","value":1}, "within": 0}
        ]}})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], json!("BAD_WINDOW"));
    assert_eq!(v["error"]["category"], json!("input"));
}

#[tokio::test]
async fn state_conflict_after_close() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let (_, _, _) = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "closed1", "ruleset": ruleset})),
        None,
    )
    .await;
    let (s, _, _) = send(&app, "POST", "/monitors/closed1/end", None, None).await;
    assert_eq!(s, StatusCode::OK);
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors/closed1/events",
        Some(json!({"kind": "x"})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], json!("MONITOR_CLOSED"));
    assert_eq!(v["error"]["category"], json!("state"));
}

#[tokio::test]
async fn duplicate_create_is_state_conflict() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let body = json!({"monitor_id": "dup", "ruleset": ruleset});
    let (s, _, _) = send(&app, "POST", "/monitors", Some(body.clone()), None).await;
    assert_eq!(s, StatusCode::OK);
    let (s, v, _) = send(&app, "POST", "/monitors", Some(body), None).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], json!("MONITOR_EXISTS"));
    assert_eq!(v["error"]["category"], json!("state"));
}

#[tokio::test]
async fn batch_is_atomic() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let _ = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "batch", "ruleset": ruleset})),
        Some("batch-run"),
    )
    .await;
    // Second event is invalid (empty object); whole batch must abort.
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors/batch/events",
        Some(json!({"events": [{"kind": "order"}, {}]})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], json!("EMPTY_EVENT"));
    let (_, v, _) = send(&app, "GET", "/monitors/batch", None, None).await;
    // Nothing committed: next_step still 0, no obligations.
    assert_eq!(v["data"]["next_step"], 0);
    assert_eq!(v["data"]["obligations_total"], 0);
}

#[tokio::test]
async fn rotate_endpoint_and_version_mismatch() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let _ = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "rot", "ruleset": ruleset})),
        None,
    )
    .await;
    let _ = send(
        &app,
        "POST",
        "/monitors/rot/events",
        Some(json!({"kind": "order"})),
        None,
    )
    .await;

    let v2: Value = serde_json::from_str(
        r#"{"version":"v2","sustain":[{"id":"s","trigger":{"all":[{"field":"kind","op":"eq","value":"go"}]},
"sustain":{"all":[{"field":"kind","op":"eq","value":"go"}]},"after":0,"duration":2}]}"#,
    )
    .unwrap();
    let (s, v, _) = send(&app, "POST", "/monitors/rot/rotate", Some(v2.clone()), None).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["data"]["new_epoch"], json!(1));
    // Rotating to the same version is an input error.
    let (s, v, _) = send(&app, "POST", "/monitors/rot/rotate", Some(v2), None).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], json!("SAME_VERSION"));
}

#[tokio::test]
async fn decisions_journal_and_verify() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let _ = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "j", "ruleset": ruleset})),
        Some("journal-run"),
    )
    .await;
    let _ = send(
        &app,
        "POST",
        "/monitors/j/events",
        Some(json!({"kind": "order"})),
        None,
    )
    .await;
    let (_, v, _) = send(&app, "GET", "/monitors/j/decisions?limit=2", None, None).await;
    assert_eq!(v["data"]["count"], json!(2));
    // All journal entries carry the run id.
    for d in v["data"]["decisions"].as_array().unwrap() {
        assert_eq!(d["run_id"], json!("journal-run"));
    }
    let (s, v, _) = send(&app, "POST", "/monitors/j/verify", None, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["verified"], json!(true));
    assert!(v["data"]["digest"].as_str().unwrap().len() == 64);
}

#[tokio::test]
async fn snapshot_restore_then_continue_agrees() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let _ = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "snap", "ruleset": ruleset})),
        Some("snap-run"),
    )
    .await;
    let _ = send(
        &app,
        "POST",
        "/monitors/snap/events",
        Some(json!({"kind": "order"})),
        None,
    )
    .await;
    let (_, snap_resp, _) = send(&app, "GET", "/monitors/snap/snapshot", None, None).await;
    let snapshot = snap_resp["data"].clone();

    // Restore into a NEW monitor id is rejected (ids must match).
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors/other/restore",
        Some(json!({"snapshot": snapshot})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], json!("SNAPSHOT_ID_MISMATCH"));

    // Restore into the same id, then finish exactly like an uninterrupted run.
    let (s, _, _) = send(
        &app,
        "POST",
        "/monitors/snap/restore",
        Some(json!({"snapshot": snapshot, "expected_version": "v1"})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let (_, before, _) = send(&app, "GET", "/monitors/snap", None, None).await;
    assert_eq!(before["data"]["next_step"], json!(1));
    assert_eq!(before["data"]["global_verdict"], json!("wait"));

    let _ = send(
        &app,
        "POST",
        "/monitors/snap/events",
        Some(json!({"kind": "payment"})),
        None,
    )
    .await;
    let (_, v, _) = send(&app, "POST", "/monitors/snap/end", None, None).await;
    assert_eq!(v["data"]["verdict"], json!("sat"));
}

#[tokio::test]
async fn tampered_snapshot_rejected_over_http() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let _ = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "tamper", "ruleset": ruleset})),
        None,
    )
    .await;
    let _ = send(
        &app,
        "POST",
        "/monitors/tamper/events",
        Some(json!({"kind": "order"})),
        None,
    )
    .await;
    let (_, snap_resp, _) = send(&app, "GET", "/monitors/tamper/snapshot", None, None).await;
    let mut snapshot = snap_resp["data"].clone();
    snapshot["next_step"] = json!(42); // digest no longer matches
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors/tamper/restore",
        Some(json!({"snapshot": snapshot})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], json!("SNAPSHOT_DIGEST_MISMATCH"));
}

#[tokio::test]
async fn resource_limit_over_http() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({
            "monitor_id": "lim",
            "ruleset": ruleset,
            "limits": {"max_active_obligations": 1, "max_epochs": 2, "max_log_bytes": 1048576}
        })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    let _ = send(
        &app,
        "POST",
        "/monitors/lim/events",
        Some(json!({"kind": "order"})),
        None,
    )
    .await;
    let (s, v, _) = send(
        &app,
        "POST",
        "/monitors/lim/events",
        Some(json!({"kind": "order"})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::INSUFFICIENT_STORAGE); // 507
    assert_eq!(v["error"]["code"], json!("OBLIGATION_LIMIT"));
    assert_eq!(v["error"]["category"], json!("resource"));
}

#[tokio::test]
async fn run_id_is_generated_when_absent_and_echoed() {
    let app = app();
    let ruleset: Value = serde_json::from_str(RULESET).unwrap();
    let (s, v, rid) = send(
        &app,
        "POST",
        "/monitors",
        Some(json!({"monitor_id": "gen", "ruleset": ruleset})),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert!(!rid.is_empty());
    assert_eq!(v["run_id"], json!(rid));
    assert_eq!(rid.len(), 36); // uuid v4
}
