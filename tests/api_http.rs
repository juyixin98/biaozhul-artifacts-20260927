//! HTTP-layer tests via in-process Axum routing (no real network port needed).
//! These assert concrete payloads and concrete error categories.

mod common;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use common::fixtures::json;
use common::{AppState, Config, SolverRegistry};
use tower::ServiceExt;

use mus_service::api::router;
use mus_service::solver::{SolveLimits, SolveOutcome, SatSolver};

/// Solver that answers the initial whole-input check with the real DPLL (so the input
/// is genuinely proved UNSAT), then stalls on deletion trials while honouring the
/// cancel flag — a deterministic stand-in for a long-running extraction.
struct StallSolver {
    pause_ms: u64,
    inner: mus_service::solver::dpll::DpllSolver,
}

impl SatSolver for StallSolver {
    fn name(&self) -> &str {
        "stall"
    }
    fn solve(
        &self,
        formula: &mus_service::language::Formula,
        mask: &[bool],
        limits: &SolveLimits,
        cancel: Option<&AtomicBool>,
    ) -> SolveOutcome {
        let is_initial_check = mask.iter().all(|b| *b);
        if is_initial_check {
            return self.inner.solve(formula, mask, limits, cancel);
        }
        // Deletion trials model a long-running solver: block until cancelled. This
        // makes the HTTP cancellation test deterministic rather than timing-based.
        loop {
            if let Some(flag) = cancel {
                if flag.load(Ordering::Relaxed) {
                    return SolveOutcome::unknown("cancellation requested", 0);
                }
            }
            std::thread::sleep(Duration::from_millis(self.pause_ms));
        }
    }
}

fn test_state() -> AppState {
    let config = Config {
        default_call_budget: None,
        default_decision_budget: None,
        ..Config::default()
    };
    AppState::new(Arc::new(config), Arc::new(SolverRegistry::built_in(24)))
}

fn state_with_stall() -> AppState {
    let config = Config {
        default_solver: "stall".into(),
        default_call_budget: None,
        default_decision_budget: None,
        ..Config::default()
    };
    let mut registry = SolverRegistry::built_in(24);
    registry.insert_custom(Arc::new(StallSolver {
        pause_ms: 2,
        inner: mus_service::solver::dpll::DpllSolver::new(),
    }));
    AppState::new(Arc::new(config), Arc::new(registry))
}

async fn call(
    state: AppState,
    method: &str,
    uri: &str,
    body: Option<&str>,
    req_id: Option<&str>,
) -> (StatusCode, serde_json::Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(rid) = req_id {
        builder = builder.header("x-request-id", rid);
    }
    let req = match body {
        Some(b) => builder
            .header("content-type", "application/json")
            .body(Body::from(b.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = router(state).oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let value: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or_else(|e| {
            panic!("non-JSON response ({e}): {}", String::from_utf8_lossy(&bytes))
        })
    };
    (status, value)
}

#[tokio::test]
async fn health_lists_solvers() {
    let (status, body) = call(test_state(), "GET", "/healthz", None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "ok");
    let solvers = body["available_solvers"].as_array().unwrap();
    assert!(solvers.iter().any(|s| s == "dpll"));
    assert!(solvers.iter().any(|s| s == "brute"));
}

#[tokio::test]
async fn sync_extract_returns_certified_core_with_concrete_ids() {
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some(json::OVERLAP_BODY),
        Some("http-req-1"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["request_id"], "http-req-1");
    assert_eq!(body["outcome"], "completed");
    let core: Vec<&str> = body["core"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(core, vec!["d", "e", "r_dup_c", "r_weak"]);
    assert_eq!(body["verification"]["verdict"], "certified");
    assert_eq!(
        body["verification"]["verifier_solver"],
        "brute"
    );
    // The audit trail records concrete trial verdicts per deletion.
    let removed_sat = body["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|d| d["action"] == "removed" && d["trial_status"] == "unsat");
    assert!(removed_sat, "expected an UNSAT-trial removal, got {body}");
}

#[tokio::test]
async fn malformed_json_is_a_named_category() {
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some("{ not json"),
        Some("http-req-2"),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["category"], "malformed_json");
    assert_eq!(body["request_id"], "http-req-2");
}

#[tokio::test]
async fn duplicate_ids_are_invalid_input() {
    let body_text = r#"{"clauses":[
        {"id":"x","literals":[1]},
        {"id":"x","literals":[-1]}
    ]}"#;
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some(body_text),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["category"], "invalid_input");
    assert!(body["error"]["message"]
        .as_str()
        .unwrap()
        .contains("duplicate clause id"));
}

#[tokio::test]
async fn unknown_solver_is_rejected_with_inventory() {
    let body_text = r#"{"clauses":[{"id":"a","literals":[1]}],"solver":"does-not-exist"}"#;
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some(body_text),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["category"], "unknown_solver");
    assert!(body["error"]["message"].as_str().unwrap().contains("dpll"));
}

#[tokio::test]
async fn satisfiable_input_returns_input_sat_category() {
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some(json::SAT_BODY),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["outcome"], "input_sat");
    assert_eq!(body["stop_reason"], "input_sat");
    assert!(body["core"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn budget_overrun_is_a_distinct_payload_outcome() {
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some(json::BUDGET_BODY),
        Some("http-req-budget"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "an over-budget run is still a report");
    assert_eq!(body["outcome"], "budget_exhausted");
    assert_eq!(body["budget_limit"], 3);
    assert_eq!(body["solver_calls"], 3);
    assert!(body.get("verification").is_none());
    assert!(body["verification_skipped_reason"]
        .as_str()
        .unwrap()
        .contains("complete run"));
}

#[tokio::test]
async fn sensitive_literals_are_redacted_in_response_and_not_derivable() {
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some(json::SENSITIVE_BODY),
        Some("http-req-secret"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let serialized = body.to_string();
    assert!(
        !serialized.contains("1111"),
        "sensitive literal leaked into payload: {serialized}"
    );
    // Clause ids are the correlation key and remain; contents must not.
    assert!(serialized.contains("secret-alpha"));
    // Redaction marker present on every decision record.
    for d in body["decisions"].as_array().unwrap() {
        assert_eq!(d["candidate"]["content"]["mode"], "redacted");
        assert!(d["candidate"]["content"]["formula_fingerprint"].is_string());
    }
}

#[tokio::test]
async fn request_id_header_is_echoed_on_errors() {
    // Empty body: the "clauses" field is structurally missing, so this is a
    // malformed-request 400 (not a 422 semantic validation failure). Either way the
    // correlation id must be echoed; assert the concrete category we actually define.
    let (status, body) = call(
        test_state(),
        "POST",
        "/extract",
        Some("{}"),
        Some("corr-42"),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["category"], "malformed_json");
    assert_eq!(body["request_id"], "corr-42");
}

#[tokio::test]
async fn missing_job_is_not_found_category() {
    let (status, body) = call(
        test_state(),
        "GET",
        "/jobs/job-does-not-exist",
        None,
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["category"], "job_not_found");
}

#[tokio::test]
async fn async_job_can_be_cancelled_and_keeps_its_partial_state() {
    let state = state_with_stall();

    // Create
    let (status, body) = call(
        state.clone(),
        "POST",
        "/jobs",
        Some(json::OVERLAP_BODY),
        Some("job-req-1"),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED, "body: {body}");
    let job_id = body["job_id"].as_str().unwrap().to_string();

    // Cancel while the blocking task is spinning
    let (cstatus, cbody) = call(
        state.clone(),
        "POST",
        &format!("/jobs/{job_id}/cancel"),
        None,
        None,
    )
    .await;
    assert_eq!(cstatus, StatusCode::ACCEPTED);
    assert_eq!(cbody["status"], "cancel_requested");

    // Poll until terminal
    let mut terminal = None;
    for _ in 0..100 {
        let (_, gbody) = call(
            state.clone(),
            "GET",
            &format!("/jobs/{job_id}"),
            None,
            None,
        )
        .await;
        let life = gbody["lifecycle"].as_str().unwrap_or("");
        let terminal_life = life == "cancelled" || life == "done";
        // The report is written in the same critical section as the terminal
        // lifecycle, so require both together rather than observing a half-updated job.
        if terminal_life && gbody["report"].is_object() {
            terminal = Some((life.to_string(), gbody));
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let (life, gbody) = terminal.expect("job reached a terminal state");
    assert_eq!(life, "cancelled", "cancelled job lifecycle, got {gbody}");
    let report = gbody["report"].as_object().expect("report retained");
    assert_eq!(report["outcome"], "cancelled");
    // A cancelled run must never masquerade as complete: no certificate is issued
    // and the per-member proof states are present (some members may already be
    // proven if trials finished before the cancel boundary was reached; later ones
    // are marked untested). Either way, no false "fully certified" claim survives.
    // `verification` is skip_serialized when absent, so it must simply be missing.
    assert!(
        report.get("verification").is_none(),
        "no certificate on a cancelled run, got {gbody}"
    );
    // Cancellation may land either before the first trial (no retained candidate yet)
    // or mid-run (some retained members, none falsely certified); both are valid.
    let proofs = report["member_proofs"].as_array().unwrap();
    // Each retained member, if any, carries an explicit per-member proof state.
    for m in proofs {
        assert!(m.get("state").is_some(), "missing proof state: {gbody}");
    }
    assert!(
        report["verification_skipped_reason"]
            .as_str()
            .unwrap_or_default()
            .contains("complete run")
        || report["summary"]
            .as_str()
            .unwrap_or_default()
            .contains("cancellation"),
        "cancellation must be explained, got {gbody}"
    );
    assert_eq!(report["request_id"], "job-req-1");
}

#[tokio::test]
async fn double_cancel_is_rejected_as_not_cancellable() {
    let state = state_with_stall();
    let (_, body) = call(
        state.clone(),
        "POST",
        "/jobs",
        Some(json::OVERLAP_BODY),
        None,
    )
    .await;
    let job_id = body["job_id"].as_str().unwrap().to_string();
    let _ = call(
        state.clone(),
        "POST",
        &format!("/jobs/{job_id}/cancel"),
        None,
        None,
    )
    .await;
    // Wait for terminal
    for _ in 0..100 {
        let (_, g) = call(state.clone(), "GET", &format!("/jobs/{job_id}"), None, None).await;
        let life = g["lifecycle"].as_str().unwrap_or("");
        if life == "cancelled" || life == "done" {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let (status, body) = call(
        state,
        "POST",
        &format!("/jobs/{job_id}/cancel"),
        None,
        None,
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["category"], "job_not_cancellable");
}
