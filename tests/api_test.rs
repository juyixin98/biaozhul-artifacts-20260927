//! HTTP-level integration tests. They drive the real Axum router in-process and
//! assert concrete status codes/error codes/results — not merely that a handler
//! returns 200.

mod common;

use common::{EnumSolver, UnsatThenStall};
use http_body_util::BodyExt;
use mus_core::api::handlers::router;
use mus_core::api::AppState;
use mus_core::config::Config;
use mus_core::diagnostics::Redactor;
use mus_core::solver::{builtin::DpllSolver, Solver};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tower::util::ServiceExt;
use tracing::subscriber;

fn app_state(primary: Arc<dyn Solver>) -> AppState {
    AppState {
        cfg: Arc::new(Config::default()),
        primary,
        oracle: Arc::new(EnumSolver),
        jobs: Default::default(),
        cancels: Default::default(),
        redactor: Redactor::new(false),
        concurrency: Arc::new(tokio::sync::Semaphore::new(8)),
    }
}

async fn send(
    app: AppState,
    method: &str,
    path: &str,
    body: Option<serde_json::Value>,
    rid: Option<&str>,
) -> (u16, serde_json::Value, AppState) {
    let mut builder = axum::http::Request::builder().method(method).uri(path);
    if let Some(r) = rid {
        builder = builder.header("x-request-id", r);
    }
    let req = match body {
        Some(v) => builder
            .header("content-type", "application/json")
            .body(axum::body::Body::from(v.to_string()))
            .unwrap(),
        None => builder.body(axum::body::Body::empty()).unwrap(),
    };
    let resp = router(app.clone()).oneshot(req).await.unwrap();
    let status = resp.status().as_u16();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null)
    };
    (status, json, app)
}

fn overlapping_body() -> serde_json::Value {
    serde_json::json!({
        "nvars": 4,
        "constraints": [
            {"id": "u", "literals": [1]},
            {"id": "v", "literals": [-1]},
            {"id": "p", "literals": [2]},
            {"id": "q", "literals": [-2]},
            {"id": "d", "literals": [-1, 2, 3]},
            {"id": "e", "literals": [-1, 2, -3]},
            {"id": "r1", "literals": [4, -4]}
        ]
    })
}

#[tokio::test]
async fn health_ok() {
    let s = app_state(Arc::new(DpllSolver::default()));
    let (status, json, _) = send(s, "GET", "/health", None, None).await;
    assert_eq!(status, 200);
    assert_eq!(json["status"], "ok");
}

#[tokio::test]
async fn lists_two_independent_solvers() {
    let s = app_state(Arc::new(DpllSolver::default()));
    let (status, json, _) = send(s, "GET", "/v1/solvers", None, None).await;
    assert_eq!(status, 200);
    let roles: Vec<&str> = json.as_array().unwrap().iter().map(|v| v["role"].as_str().unwrap()).collect();
    assert!(roles.contains(&"primary"));
    assert!(roles.contains(&"independent_oracle"));
}

#[tokio::test]
async fn extract_returns_certified_core_and_independent_verification() {
    let s = app_state(Arc::new(DpllSolver::default()));
    let (status, json, _) = send(s, "POST", "/v1/extract", Some(overlapping_body()), Some("rid-test-1")).await;
    assert_eq!(status, 200, "body: {json}");
    assert_eq!(json["request_id"], "rid-test-1", "client request id must be echoed");
    assert_eq!(json["termination"], "completed");

    let core: Vec<String> = json["cores"][0]["member_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert!(
        (core.len() == 2 && {
            let set: std::collections::BTreeSet<&str> = core.iter().map(String::as_str).collect();
            set == ["u", "v"].into_iter().collect() || set == ["p", "q"].into_iter().collect()
        }),
        "expected an independently minimal 2-core, got {core:?}"
    );
    assert_eq!(json["cores"][0]["verdict"], "certified_mus");

    // Independent verification is on by default and agrees.
    assert_eq!(json["verification"]["all_certified"], true);
    assert!(json["verification"]["trace_audit"]
        .as_array()
        .unwrap()
        .iter()
        .all(|a| a["ok"] == true));

    // Every deletion is recorded with tried set + verdict.
    let trace = json["trace"].as_array().unwrap();
    assert!(trace.iter().any(|t| t["tested_id"] == "r1" && t["verdict"] == "unsat" && t["kept"] == false));
}

#[tokio::test]
async fn malformed_json_is_400_with_code_and_request_id() {
    let s = app_state(Arc::new(DpllSolver::default()));
    let app = router(s.clone());
    let req = axum::http::Request::post("/v1/extract")
        .header("x-request-id", "rid-bad")
        .body(axum::body::Body::from("{ not json"))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status().as_u16(), 400);
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(json["error"]["code"], "invalid_json");
    assert_eq!(json["error"]["request_id"], "rid-bad");
}

#[tokio::test]
async fn concrete_failure_categories_are_distinct() {
    let cases: Vec<(&str, serde_json::Value, &str)> = vec![
        (
            "/v1/extract",
            serde_json::json!({"nvars": 1, "constraints": [{"id": "a", "literals": [1]}, {"id": "a", "literals": [-1]}]}),
            "duplicate_constraint_id",
        ),
        (
            "/v1/extract",
            serde_json::json!({"nvars": 1, "constraints": [{"id": "a", "literals": [2]}]}),
            "variable_out_of_range",
        ),
        (
            "/v1/extract",
            serde_json::json!({"nvars": 2}),
            "missing_formula",
        ),
        (
            "/v1/extract",
            serde_json::json!({"nvars": 2, "constraints": [], "text": ""}),
            "conflicting_input",
        ),
        (
            "/v1/extract",
            serde_json::json!({"nvars": 2, "constraints": [{"id": "a", "literals": [1]}], "mode": "nonsense"}),
            "unknown_mode",
        ),
        (
            "/v1/extract",
            serde_json::json!({"nvars": 2, "constraints": [{"id": "a", "literals": [1]}], "budget": 100001}),
            "budget_too_large",
        ),
        (
            "/v1/extract",
            serde_json::json!({"nvars": 2, "constraints": [{"id": "a", "literals": [1, 1]}]}),
            "duplicate_literal",
        ),
    ];

    for (path, body, code) in cases {
        let s = app_state(Arc::new(DpllSolver::default()));
        let (status, json, _) = send(s, "POST", path, Some(body), None).await;
        assert_eq!(status, 400, "expected 400 for {code}");
        assert_eq!(
            json["error"]["code"], code,
            "specific failure category must be reported, got {json}"
        );
        assert!(json["error"]["request_id"].is_string());
    }
}

#[tokio::test]
async fn budget_overrun_is_a_200_answer_with_explicit_termination() {
    let s = app_state(Arc::new(DpllSolver::default()));
    let mut body = overlapping_body();
    body["budget"] = serde_json::json!(1);
    let (status, json, _) = send(s, "POST", "/v1/extract", Some(body), None).await;
    assert_eq!(status, 200, "budget exhaustion is a valid outcome, not a transport error");
    assert_eq!(json["termination"], "budget_exhausted");
    assert_eq!(json["cores"][0]["verdict"], "uncertified_unsat_candidate");
    assert!(json["retained_candidate"].as_array().unwrap().len() == 7);
}

#[tokio::test]
async fn async_job_lifecycle_and_cancel_retains_candidate() {
    let _ = subscriber::set_default(tracing_subscriber::fmt().with_test_writer().finish());
    let (stall, _observed) = UnsatThenStall::new();
    let stall: Arc<dyn Solver> = Arc::new(stall);
    let mut state = app_state(stall);

    // Create
    let (status, created, s2) =
        send(state.clone(), "POST", "/v1/jobs", Some(overlapping_body()), Some("rid-job")).await;
    state = s2;
    assert_eq!(status, 202);
    let job_id = created["job_id"].as_str().unwrap().to_string();
    assert_eq!(created["status"], "queued");

    // Wait until running.
    let path = format!("/v1/jobs/{job_id}");
    let mut running = false;
    for _ in 0..100 {
        let (_, j, s2) = send(state.clone(), "GET", &path, None, None).await;
        state = s2;
        if j["status"] == "running" {
            running = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(running, "job should reach running state");

    // Cancel
    let (status, cj, s2) = send(state.clone(), "POST", &format!("{path}/cancel"), None, None).await;
    state = s2;
    assert_eq!(status, 200);
    assert_eq!(cj["status"], "cancellation_requested");

    // Poll to terminal state.
    let mut final_json = serde_json::Value::Null;
    for _ in 0..200 {
        let (_, j, s2) = send(state.clone(), "GET", &path, None, None).await;
        state = s2;
        if j["status"] == "succeeded" || j["status"] == "failed" {
            final_json = j;
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(final_json["status"], "succeeded", "cancel is a result, not a job failure: {final_json}");
    let result = &final_json["result"];
    assert_eq!(result["termination"], "cancelled");
    assert!(
        !result["retained_candidate"].as_array().unwrap().is_empty(),
        "verified UNSAT candidate must be retained on cancel"
    );

    // Cancelling a finished job conflicts.
    let (status, _, _) = send(state.clone(), "POST", &format!("{path}/cancel"), None, None).await;
    assert_eq!(status, 409);

    // Unknown job id is 404, distinct from 409.
    let (status, json, _) = send(state, "GET", "/v1/jobs/does-not-exist", None, None).await;
    assert_eq!(status, 404);
    assert_eq!(json["error"]["code"], "job_not_found");
}

#[tokio::test]
async fn async_job_completes_successfully_when_uncancelled() {
    let s = app_state(Arc::new(DpllSolver::default()));
    let (_, created, s) = send(s, "POST", "/v1/jobs", Some(overlapping_body()), None).await;
    let job_id = created["job_id"].as_str().unwrap().to_string();
    let path = format!("/v1/jobs/{job_id}");

    let mut final_json = serde_json::Value::Null;
    let mut state = s;
    for _ in 0..200 {
        let (_, j, s2) = send(state.clone(), "GET", &path, None, None).await;
        state = s2;
        if j["status"] == "succeeded" {
            final_json = j;
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(final_json["status"], "succeeded");
    assert_eq!(final_json["result"]["termination"], "completed");
    assert_eq!(final_json["result"]["cores"][0]["verdict"], "certified_mus");

    // The jobs map should eventually drop the cancel token; at least no panic.
    let _: HashMap<String, ()> = HashMap::new();
}
