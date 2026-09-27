//! HTTP-level integration tests using the real Axum router with an
//! in-memory store (no network sockets: requests go through tower oneshot).

mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use bounded_monitor::api::router;
use bounded_monitor::store::Store;
use common::log_event;
use http_body_util::BodyExt;
use tower::util::ServiceExt;

fn app() -> axum::Router {
    router(std::sync::Arc::new(Store::new()))
}

async fn call(app: &axum::Router, method: &str, uri: &str, body: Option<serde_json::Value>, run_id: &str) -> (StatusCode, serde_json::Value, String) {
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .header("x-run-id", run_id);
    let req = match body {
        Some(v) => builder.body(Body::from(serde_json::to_vec(&v).unwrap())).unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let returned_run = resp
        .headers()
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value =
        serde_json::from_slice(&bytes).unwrap_or_else(|_| panic!("non-json response: {bytes:?}"));
    (status, json, returned_run)
}

fn ruleset_json() -> serde_json::Value {
    serde_json::to_value(common::load_ruleset("shop-v1.json")).unwrap()
}

#[tokio::test]
async fn full_lifecycle_with_snapshot_and_recovery() {
    let app = app();
    let rid = "api-lifecycle";

    // create
    let (status, body, returned) = call(
        &app,
        "POST",
        "/monitors",
        Some(serde_json::json!({ "monitor_id": "m1", "ruleset": ruleset_json() })),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(returned, rid, "server must echo the supplied run id");
    assert_eq!(body["data"]["monitor_id"], "m1");
    assert!(body["data"]["ruleset_hash"].is_string());
    assert_eq!(body["data"]["verdict"], "pending");

    // step 0: order_placed
    let steps = common::load_trace("a_boundary_satisfied.json");
    for s in &steps[..3] {
        let (status, body, _) =
            call(&app, "POST", "/monitors/m1/steps", Some(serde_json::to_value(s).unwrap()), rid).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        log_event(
            rid,
            "api_lifecycle",
            "step",
            format!("step {} verdict={}", body["data"]["index"], body["data"]["verdict"]),
        );
    }

    // snapshot after 3 steps
    let (status, snap_body, _) = call(&app, "GET", "/monitors/m1/snapshot", None, rid).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(snap_body["data"]["step_count"], 3);
    let snapshot = snap_body["data"].clone();

    // finish the trace on the original monitor and remember the verdict
    for s in &steps[3..] {
        let (status, _, _) =
            call(&app, "POST", "/monitors/m1/steps", Some(serde_json::to_value(s).unwrap()), rid).await;
        assert_eq!(status, StatusCode::OK);
    }
    let (status, final_body, _) = call(&app, "GET", "/monitors/m1", None, rid).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(final_body["data"]["verdict"], "satisfied");
    assert_eq!(final_body["data"]["closed"], true);

    // restore the 3-step snapshot into a new monitor and drive the same
    // suffix: recovery must reach the same verdict.
    let (status, restored, _) = call(
        &app,
        "POST",
        "/restore",
        Some(serde_json::json!({
            "monitor_id": "m1-resumed",
            "ruleset": ruleset_json(),
            "snapshot": snapshot,
        })),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{restored}");
    for s in &steps[3..] {
        let (status, body, _) = call(
            &app,
            "POST",
            "/monitors/m1-resumed/steps",
            Some(serde_json::to_value(s).unwrap()),
            rid,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }
    let (status, resumed_final, _) = call(&app, "GET", "/monitors/m1-resumed", None, rid).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(resumed_final["data"]["verdict"], "satisfied");
    assert_eq!(
        resumed_final["data"]["obligations"],
        final_body["data"]["obligations"],
        "recovered obligations must equal the never-restarted run"
    );
    log_event(rid, "api_lifecycle", "recovery_consistent", "resumed monitor obligations equal original run");
}

#[tokio::test]
async fn endpoint_close_seals_pending_obligations() {
    let app = app();
    let rid = "api-close";
    let (status, _, _) = call(
        &app,
        "POST",
        "/monitors",
        Some(serde_json::json!({ "monitor_id": "m2", "ruleset": ruleset_json() })),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::OK);

    let trigger = common::load_trace("d_early_close.json");
    let (status, body, _) = call(
        &app,
        "POST",
        "/monitors/m2/steps",
        Some(serde_json::to_value(&trigger[0]).unwrap()),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["data"]["verdict"], "pending");

    // POST /close appends the bare end marker itself.
    let (status, body, _) = call(&app, "POST", "/monitors/m2/close", None, rid).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["data"]["closed"], true);
    assert_eq!(body["data"]["verdict"], "violated");

    // Further steps are a state conflict.
    let (status, body, _) = call(
        &app,
        "POST",
        "/monitors/m2/steps",
        Some(serde_json::json!({ "index": 2, "event": { "type": "x", "facts": {} } })),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error_kind"], "state_conflict");
    assert_eq!(body["reason"], "monitor_closed");
    log_event(rid, "api_close", "closed_conflict", body["detail"].as_str().unwrap_or("").to_string());
}

#[tokio::test]
async fn four_error_classes_map_to_distinct_status_and_kind() {
    let app = app();

    // input error: malformed JSON -> 400 input_error/malformed_json
    let req = Request::builder()
        .method("POST")
        .uri("/monitors")
        .header("content-type", "application/json")
        .header("x-run-id", "api-err-input")
        .body(Body::from("{not json"))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["error_kind"], "input_error");
    assert_eq!(body["reason"], "malformed_json");

    // input error: zero window -> 400 input_error/zero_window
    let mut bad_ruleset: serde_json::Value = ruleset_json();
    bad_ruleset["rules"][0]["within_steps"] = 0.into();
    let (status, body, _) = call(
        &app,
        "POST",
        "/monitors",
        Some(serde_json::json!({ "monitor_id": "bad", "ruleset": bad_ruleset })),
        "api-err-input2",
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error_kind"], "input_error");
    assert_eq!(body["reason"], "zero_window");

    // state conflict: unknown monitor -> 409 state_conflict/unknown_monitor
    let (status, body, _) = call(&app, "GET", "/monitors/nope", None, "api-err-state").await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error_kind"], "state_conflict");
    assert_eq!(body["reason"], "unknown_monitor");

    // resource exhaustion: max_monitors = 0 on a fresh store
    let tiny = router(std::sync::Arc::new(Store::new()));
    let (status, body, _) = call(
        &tiny,
        "POST",
        "/monitors",
        Some(serde_json::json!({
            "monitor_id": "z",
            "ruleset": ruleset_json(),
            "limits": { "max_steps": 10, "max_obligations_total": 10, "max_obligations_per_step": 10, "max_monitors": 0 }
        })),
        "api-err-resource",
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error_kind"], "resource_exhausted");
    assert_eq!(body["reason"], "max_monitors_exceeded");

    // computation failure: non-numeric fact compared numerically.
    let mut rs: serde_json::Value = ruleset_json();
    rs["rules"] = serde_json::json!([
        {
            "type": "sustain",
            "id": "num",
            "condition": { "op": "gt", "path": "amount", "value": 1 },
            "duration_steps": 1,
            "scope": { "mode": "always" }
        },
        {
            "type": "sustain",
            "id": "padding",
            "condition": { "op": "eq", "path": "x", "value": true },
            "duration_steps": 1,
            "scope": { "mode": "always" }
        }
    ]);
    let (status, _, _) = call(
        &app,
        "POST",
        "/monitors",
        Some(serde_json::json!({ "monitor_id": "num", "ruleset": rs })),
        "api-err-compute-create",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (status, body, _) = call(
        &app,
        "POST",
        "/monitors/num/steps",
        Some(serde_json::json!({
            "index": 0,
            "event": { "type": "x", "facts": { "amount": "oops", "x": true } }
        })),
        "api-err-compute",
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error_kind"], "computation_failed");
    assert_eq!(body["reason"], "non_numeric_fact");
    log_event("api-err-compute", "api_errors", "computation_failed", body["detail"].as_str().unwrap_or("").to_string());
}

#[tokio::test]
async fn offline_evaluate_and_evidence_endpoints_agree() {
    let app = app();
    let rid = "api-evaluate";
    let trace = common::load_trace("b_overlap_triggers.json");

    let (status, body, _) = call(
        &app,
        "POST",
        "/evaluate",
        Some(serde_json::json!({ "ruleset": ruleset_json(), "trace": trace })),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["data"]["verdict"], "violated");
    assert_eq!(body["data"]["closed"], true);

    // Drive the same trace live and fetch evidence with a cut, then verify.
    let (status, _, _) = call(
        &app,
        "POST",
        "/monitors",
        Some(serde_json::json!({ "monitor_id": "ev", "ruleset": ruleset_json() })),
        rid,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    for s in &trace[..3] {
        let (status, _, _) =
            call(&app, "POST", "/monitors/ev/steps", Some(serde_json::to_value(s).unwrap()), rid).await;
        assert_eq!(status, StatusCode::OK);
    }
    let (status, evidence, _) = call(&app, "GET", "/monitors/ev/evidence?snapshot_after=3", None, rid).await;
    assert_eq!(status, StatusCode::OK, "{evidence}");
    let bundle = evidence["data"].clone();
    assert_eq!(bundle["snapshot_after_index"], 3);

    // Feed only the prefix steps into the live monitor but verify the full
    // trace bundle — the verifier restores and re-drives, so finish driving
    // first to keep the claimed verdict consistent.
    for s in &trace[3..] {
        let (status, _, _) =
            call(&app, "POST", "/monitors/ev/steps", Some(serde_json::to_value(s).unwrap()), rid).await;
        assert_eq!(status, StatusCode::OK);
    }
    let (status, evidence, _) = call(&app, "GET", "/monitors/ev/evidence?snapshot_after=3", None, rid).await;
    assert_eq!(status, StatusCode::OK);
    let (status, report, _) =
        call(&app, "POST", "/verify", Some(evidence["data"].clone()), rid).await;
    assert_eq!(status, StatusCode::OK, "{report}");
    assert_eq!(report["data"]["valid"], true, "evidence must verify: {report}");
    assert_eq!(report["data"]["oracle_verdict"], "violated");
    assert_eq!(report["data"]["restored_verdict"], "violated");
    log_event(rid, "api_evaluate", "verified", "live evidence bundle verified after cut+restore");
}

#[tokio::test]
async fn health_and_run_id_generation() {
    let app = app();
    let req = Request::builder().method("GET").uri("/health").body(Body::empty()).unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    // A request without x-run-id still gets a generated run id back.
    let req = Request::builder()
        .method("GET")
        .uri("/monitors/missing")
        .body(Body::empty())
        .unwrap();
    let resp = router(std::sync::Arc::new(Store::new())).oneshot(req).await.unwrap();
    let rid = resp.headers().get("x-run-id").unwrap().to_str().unwrap().to_string();
    assert!(rid.starts_with("run-"), "generated run id was {rid}");
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}
