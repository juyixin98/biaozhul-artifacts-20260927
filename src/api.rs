//! HTTP backend (Axum).
//!
//! Every response — success or failure — carries a `run_id` (header
//! `x-run-id` plus body field) that also appears in test logs, so a reported
//! problem can be replayed by run id.  The four error classes map to
//! distinct HTTP status codes (see [`ErrorKind::http_status`]).

use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::error::{AppError, AppResult, ErrorKind};
use crate::evidence::EvidenceBundle;
use crate::kernel::{Limits, Monitor, SavedMonitor, Verdict};
use crate::language::{Ruleset, Step};
use crate::oracle::{self, OracleInstance};
use crate::store::Store;

#[derive(Clone)]
pub struct AppState {
    pub store: std::sync::Arc<Store>,
}

/// Build the application router (also used by tests via tower oneshot).
pub fn router(store: std::sync::Arc<Store>) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/health", get(health))
        .route("/monitors", post(create_monitor))
        .route("/monitors/:id", get(get_monitor))
        .route("/monitors/:id/steps", post(post_step))
        .route("/monitors/:id/close", post(close_monitor))
        .route("/monitors/:id/snapshot", get(get_snapshot))
        .route("/monitors/:id/evidence", get(get_evidence))
        .route("/restore", post(restore_snapshot))
        .route("/evaluate", post(offline_evaluate))
        .route("/verify", post(verify_bundle))
        .with_state(state)
}

// ---------- envelopes ----------

#[derive(Debug, Serialize)]
struct Envelope<T: Serialize> {
    run_id: String,
    data: T,
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    run_id: String,
    error_kind: ErrorKind,
    reason: String,
    detail: String,
}

fn new_run_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
    let ptr = &nanos as *const u32 as usize;
    format!("run-{:016x}-{:08x}", ptr as u64, nanos)
}

fn run_id(headers: &HeaderMap) -> String {
    headers
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.trim().is_empty() && s.len() <= 128)
        .map(|s| s.to_string())
        .unwrap_or_else(new_run_id)
}

fn ok<T: Serialize>(run_id: String, data: T) -> Response {
    let mut resp = Json(Envelope { run_id: run_id.clone(), data }).into_response();
    resp.headers_mut().insert("x-run-id", run_id.parse().unwrap());
    resp
}

fn error_response(run_id: String, err: AppError) -> Response {
    let status = StatusCode::from_u16(err.kind.http_status()).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY);
    let body = ErrorBody {
        run_id: run_id.clone(),
        error_kind: err.kind,
        reason: err.reason.to_string(),
        detail: err.detail,
    };
    let mut resp = (status, Json(body)).into_response();
    if let Ok(v) = run_id.parse() {
        resp.headers_mut().insert("x-run-id", v);
    }
    resp
}

/// Parse a request body as JSON, mapping failures to `malformed_json` input
/// errors instead of Axum's default 400 text response.
fn parse<T: for<'de> Deserialize<'de>>(run_id: &str, bytes: &[u8]) -> AppResult<T> {
    serde_json::from_slice::<T>(bytes)
        .map_err(|e| AppError::input("malformed_json", format!("body at run {run_id}: {e}")))
}

// ---------- DTOs ----------

#[derive(Debug, Deserialize)]
struct CreateRequest {
    #[serde(default)]
    monitor_id: Option<String>,
    ruleset: Ruleset,
    #[serde(default)]
    limits: Option<Limits>,
}

#[derive(Debug, Serialize)]
struct CreatedData {
    monitor_id: String,
    ruleset_hash: String,
    verdict: Verdict,
}

#[derive(Debug, Deserialize)]
struct RestoreRequest {
    #[serde(default)]
    monitor_id: Option<String>,
    ruleset: Ruleset,
    snapshot: SavedMonitor,
}

#[derive(Debug, Deserialize)]
struct EvaluateRequest {
    ruleset: Ruleset,
    trace: Vec<Step>,
}

#[derive(Debug, Serialize)]
struct EvaluateData {
    closed: bool,
    verdict: Verdict,
    obligations: Vec<OracleInstance>,
}

#[derive(Debug, Deserialize)]
struct EvidenceQuery {
    #[serde(default)]
    snapshot_after: Option<u64>,
}

// ---------- handlers ----------

async fn health() -> &'static str {
    "ok"
}

async fn create_monitor(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let req: CreateRequest = match parse(&rid, &body) {
        Ok(v) => v,
        Err(e) => return error_response(rid, e),
    };
    let id = req.monitor_id.unwrap_or_else(|| format!("mon-{}", &new_run_id()[4..]));
    let limits = req.limits.unwrap_or_default();
    match state.store.create(id.clone(), req.ruleset, limits) {
        Ok(monitor) => {
            let hash = monitor.ruleset_hash.clone();
            let verdict = monitor.verdict();
            ok(rid, CreatedData { monitor_id: id, ruleset_hash: hash, verdict })
        }
        Err(e) => error_response(rid, e),
    }
}

async fn get_monitor(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match state.store.get(&id) {
        Ok(snap) => ok(rid, snap),
        Err(e) => error_response(rid, e),
    }
}

async fn post_step(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    body: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let step: Step = match parse(&rid, &body) {
        Ok(v) => v,
        Err(e) => return error_response(rid, e),
    };
    match state.store.apply_step(&id, &step) {
        Ok(outcome) => ok(rid, outcome),
        Err(e) => error_response(rid, e),
    }
}

async fn close_monitor(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match state.store.close(&id) {
        Ok(outcome) => ok(rid, outcome),
        Err(e) => error_response(rid, e),
    }
}

async fn get_snapshot(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match state.store.snapshot(&id) {
        Ok(snapshot) => ok(rid, snapshot),
        Err(e) => error_response(rid, e),
    }
}

async fn get_evidence(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(q): Query<EvidenceQuery>,
) -> Response {
    let rid = run_id(&headers);
    match state.store.evidence(&id, &rid, q.snapshot_after) {
        Ok(bundle) => ok(rid, bundle),
        Err(e) => error_response(rid, e),
    }
}

async fn restore_snapshot(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let req: RestoreRequest = match parse(&rid, &body) {
        Ok(v) => v,
        Err(e) => return error_response(rid, e),
    };
    let result = Monitor::restore(req.snapshot, &req.ruleset).and_then(|monitor| {
        let id = req.monitor_id.unwrap_or_else(|| format!("mon-{}", &new_run_id()[4..]));
        let hash = monitor.ruleset_hash.clone();
        let verdict = monitor.verdict();
        // Register the restored monitor as a live entry.
        state.store.insert_restored(id.clone(), req.ruleset, monitor)?;
        Ok(CreatedData { monitor_id: id, ruleset_hash: hash, verdict })
    });
    match result {
        Ok(data) => ok(rid, data),
        Err(e) => error_response(rid, e),
    }
}

async fn offline_evaluate(
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let req: EvaluateRequest = match parse(&rid, &body) {
        Ok(v) => v,
        Err(e) => return error_response(rid, e),
    };
    match oracle::evaluate(&req.ruleset, &req.trace) {
        Ok(report) => ok(
            rid,
            EvaluateData {
                closed: report.closed,
                verdict: report.verdict,
                obligations: report.obligations,
            },
        ),
        Err(e) => error_response(rid, e),
    }
}

async fn verify_bundle(headers: HeaderMap, body: Bytes) -> Response {
    let rid = run_id(&headers);
    let bundle: EvidenceBundle = match parse(&rid, &body) {
        Ok(v) => v,
        Err(e) => return error_response(rid, e),
    };
    // The bundle's own `run_id` records the producing run; the envelope
    // carries this verification run's id instead.
    match bundle.verify() {
        Ok(report) => ok(rid, report),
        Err(e) => error_response(rid, e),
    }
}
