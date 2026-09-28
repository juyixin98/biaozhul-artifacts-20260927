//! Axum application: request/response types, request-id correlation,
//! `/health`, `/version`, `/check`, `/evidence/replay`.
//!
//! Every response carries the `x-request-id` header, echoed from the client
//! or locally generated. Failures and inconclusive results are reported in
//! dedicated fields rather than folded into the verdict.

use crate::config::BudgetConfig;
use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, HeaderName, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use fsm_core::{explore, Budget};
use fsm_evidence::replay;
use fsm_lang::evidence::Evidence;
use fsm_lang::fingerprint;
use fsm_lang::{CompiledSpec, Spec};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const ALGORITHM: &str = "explicit-state BFS (deduplicated, declaration-order)";

#[derive(Clone)]
pub struct AppState {
    pub default_budget: Arc<BudgetConfig>,
}

pub fn app(default_budget: BudgetConfig) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/version", get(version))
        .route("/check", post(check))
        .route("/evidence/replay", post(evidence_replay))
        .layer(axum::middleware::from_fn(request_id_layer))
        .with_state(AppState {
            default_budget: Arc::new(default_budget),
        })
}

/// Request-scoped correlation id.
#[derive(Clone, Debug)]
pub struct RequestId(pub String);

const REQUEST_ID_HEADER: HeaderName = HeaderName::from_static("x-request-id");

fn new_request_id() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("local-{nanos:x}-{n}")
}

async fn request_id_layer(
    headers: HeaderMap,
    mut request: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
) -> Response {
    let id = headers
        .get(&REQUEST_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(new_request_id);
    tracing::info!(request_id = %id, method = %request.method(), uri = %request.uri(), "request received");
    request.extensions_mut().insert(RequestId(id.clone()));
    let span = tracing::info_span!("handle", request_id = %id);
    let mut response = {
        let _guard = span.enter();
        next.run(request).await
    };
    if let Ok(v) = HeaderValue::from_str(&id) {
        response.headers_mut().insert(REQUEST_ID_HEADER.clone(), v);
    }
    tracing::info!(
        request_id = %id,
        status = %response.status().as_u16(),
        "request completed"
    );
    response
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

async fn version() -> Json<serde_json::Value> {
    Json(json!({
        "engine": "local-fsm-checker",
        "version": ENGINE_VERSION,
        "algorithm": ALGORITHM,
        "logics": ["AG safety invariants", "EF reachability"],
    }))
}

/// Incoming check request.
#[derive(Debug, Deserialize)]
pub struct CheckRequest {
    pub spec: serde_json::Value,
    #[serde(default)]
    pub budget: Option<BudgetOverride>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
pub struct BudgetOverride {
    pub max_states: Option<u64>,
    pub max_transitions: Option<u64>,
    pub max_initial_scan: Option<u64>,
}

/// Failure block, kept separate from uncertain verdicts.
#[derive(Debug, Serialize)]
struct FailureBlock {
    code: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    transition: Option<String>,
}

#[derive(Debug, Serialize)]
struct CheckResponse {
    request_id: String,
    engine_version: String,
    algorithm: String,
    spec_name: Option<String>,
    spec_fingerprint: Option<String>,
    status: String,
    reason: String,
    truncated: bool,
    stats: Option<serde_json::Value>,
    properties: Vec<serde_json::Value>,
    deadlocks: Vec<Evidence>,
    terminals: Vec<Evidence>,
    /// Independent replay verification for each piece of evidence produced.
    evidence_checks: Vec<serde_json::Value>,
    failure: Option<FailureBlock>,
}

struct ApiError {
    status: StatusCode,
    code: String,
    message: String,
}

impl ApiError {
    fn bad_request(code: &str, message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: code.to_string(),
            message: message.into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(json!({
            "error": { "code": self.code, "message": self.message }
        }));
        (self.status, body).into_response()
    }
}

fn rid(request: &axum::http::Request<axum::body::Body>) -> String {
    request
        .extensions()
        .get::<RequestId>()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| "unknown".to_string())
}

async fn check(
    State(app): State<AppState>,
    req: axum::http::Request<axum::body::Body>,
) -> Result<Json<CheckResponse>, ApiError> {
    let request_id = rid(&req);
    let body = axum::body::to_bytes(req.into_body(), 4 * 1024 * 1024)
        .await
        .map_err(|e| ApiError::bad_request("BODY_ERROR", e.to_string()))?;
    let parsed: CheckRequest = serde_json::from_slice(&body)
        .map_err(|e| ApiError::bad_request("INVALID_JSON", format!("malformed request JSON: {e}")))?;

    let spec_model: Spec = serde_json::from_value(parsed.spec.clone()).map_err(|e| {
        ApiError::bad_request("INVALID_SPEC", format!("specification does not match schema: {e}"))
    })?;
    let (fingerprint_hex, _canonical) = fingerprint::fingerprint(&spec_model)
        .map_err(|e| ApiError::bad_request("INVALID_SPEC", format!("cannot fingerprint spec: {e}")))?;
    let compiled = CompiledSpec::compile(&spec_model).map_err(|e| {
        tracing::warn!(request_id = %request_id, code = %e.code, "specification rejected");
        ApiError::bad_request(&e.code, e.message)
    })?;

    let defaults = app.default_budget;
    let budget = Budget {
        max_states: parsed
            .budget
            .and_then(|b| b.max_states)
            .unwrap_or(defaults.max_states),
        max_transitions: parsed
            .budget
            .and_then(|b| b.max_transitions)
            .unwrap_or(defaults.max_transitions),
        max_initial_scan: parsed
            .budget
            .and_then(|b| b.max_initial_scan)
            .unwrap_or(defaults.max_initial_scan),
    };

    tracing::info!(
        request_id = %request_id,
        spec = %compiled.name,
        fingerprint = %fingerprint_hex,
        budget_states = budget.max_states,
        budget_transitions = budget.max_transitions,
        "exploration start"
    );
    let outcome = explore(&compiled, &budget);
    tracing::info!(
        request_id = %request_id,
        status = ?outcome.status,
        reason = %outcome.reason,
        discovered = outcome.stats.discovered,
        explored = outcome.stats.explored,
        truncated = outcome.truncated,
        "exploration end"
    );

    // Independently replay every returned trace.
    let mut evidence_checks = Vec::new();
    for p in &outcome.properties {
        if let Some(ev) = &p.evidence {
            let report = replay(&compiled, ev);
            evidence_checks.push(json!({
                "property": p.name,
                "kind": ev.kind,
                "replay_valid": report.valid,
                "steps": report.steps.len(),
                "failure": report.failure,
            }));
        }
    }
    for ev in outcome
        .deadlock_evidence
        .iter()
        .chain(outcome.terminal_evidence.iter())
    {
        let report = replay(&compiled, ev);
        evidence_checks.push(json!({
            "property": null,
            "kind": ev.kind,
            "replay_valid": report.valid,
            "steps": report.steps.len(),
            "failure": report.failure,
        }));
    }

    let properties = outcome
        .properties
        .iter()
        .map(|p| serde_json::to_value(p).unwrap_or_default())
        .collect();

    let failure = outcome.error.map(|e| FailureBlock {
        code: e.code,
        message: e.message,
        transition: e.transition,
    });

    Ok(Json(CheckResponse {
        request_id,
        engine_version: ENGINE_VERSION.to_string(),
        algorithm: ALGORITHM.to_string(),
        spec_name: Some(compiled.name.clone()),
        spec_fingerprint: Some(fingerprint_hex),
        status: format!("{:?}", outcome.status).to_lowercase(),
        reason: outcome.reason.clone(),
        truncated: outcome.truncated,
        stats: Some(serde_json::to_value(outcome.stats).unwrap_or_default()),
        properties,
        deadlocks: outcome.deadlock_evidence,
        terminals: outcome.terminal_evidence,
        evidence_checks,
        failure,
    }))
}

#[derive(Debug, Deserialize)]
struct ReplayRequest {
    spec: serde_json::Value,
    evidence: Evidence,
}

async fn evidence_replay(
    req: axum::http::Request<axum::body::Body>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let request_id = rid(&req);
    let body: Bytes = axum::body::to_bytes(req.into_body(), 4 * 1024 * 1024)
        .await
        .map_err(|e| ApiError::bad_request("BODY_ERROR", e.to_string()))?;
    let parsed: ReplayRequest = serde_json::from_slice(&body)
        .map_err(|e| ApiError::bad_request("INVALID_JSON", format!("malformed request JSON: {e}")))?;
    let spec_model: Spec = serde_json::from_value(parsed.spec)
        .map_err(|e| ApiError::bad_request("INVALID_SPEC", e.to_string()))?;
    let compiled = CompiledSpec::compile(&spec_model)
        .map_err(|e| ApiError::bad_request(&e.code, e.message))?;
    let report = replay(&compiled, &parsed.evidence);
    tracing::info!(
        request_id = %request_id,
        valid = report.valid,
        kind = %report.evidence_kind,
        "evidence replay"
    );
    Ok(Json(json!({
        "request_id": request_id,
        "report": report,
    })))
}
